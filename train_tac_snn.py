import torch
import numpy as np
import slayerSNN as snn
from pathlib import Path
import logging
from models.slayer_baseline import SlayerMLP
from torch.utils.data import DataLoader
from dataset import ViTacDataset
from torch.utils.tensorboard import SummaryWriter
import argparse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

log = logging.getLogger()

parser = argparse.ArgumentParser("Train model.")
parser.add_argument("--epochs", type=int, help="Number of epochs.", required=True)
parser.add_argument("--data_dir", type=str, help="Path to data.", required=True)
parser.add_argument(
    "--checkpoint_dir", type=str, help="Path for saving checkpoints.", required=True
)
parser.add_argument("--tsample", type=int, help="tSample", required=True)
parser.add_argument(
    "--tsr_stop", type=int, help="Target Spike Region Stop", required=True
)
parser.add_argument("--sc_true", type=int, help="Spike Count True", required=True)
parser.add_argument("--sc_false", type=int, help="Spike Count False", required=True)
parser.add_argument("--lr", type=float, help="Learning rate.", required=True)
parser.add_argument(
    "--sample_file", type=int, help="Sample number to train from.", required=True
)
parser.add_argument(
    "--r_l1", type=float, help="Regularization weight for L1.", required=True
)
parser.add_argument(
    "--r_l2", type=float, help="Regularization weight for L2.", required=True
)
parser.add_argument(
    "--r_fano", type=float, help="Regularization weight for Fano.", required=True
)
parser.add_argument(
    "--r_fano_var", type=float, help="Regularization weight for Fano (Variance Only).", required=True
)
parser.add_argument(
    "--hidden_size", type=int, help="Size of hidden layer.", required=True
)
parser.add_argument(
    "--batch_size", type=int, help="Batch Size.", required=True
)

args = parser.parse_args()

params = {
    "neuron": {
        "type": "SRMALPHA",
        "theta": 10,
        "tauSr": 10.0,
        "tauRef": 1.0,
        "scaleRef": 2,
        "tauRho": 1,
        "scaleRho": 1,
    },
    "simulation": {"Ts": 1.0, "tSample": args.tsample, "nSample": 1},
    "training": {
        "error": {
            "type": "NumSpikes",  # "NumSpikes" or "ProbSpikes"
            "probSlidingWin": 20,  # only valid for ProbSpikes
            "tgtSpikeRegion": {  # valid for NumSpikes and ProbSpikes
                "start": 0,
                "stop": args.tsr_stop,
            },
            "tgtSpikeCount": {True: args.sc_true, False: args.sc_false},
        }
    },
}

input_size = 156  # Tact
output_size = 20

device = torch.device("cuda")
writer = SummaryWriter(".")
net = SlayerMLP(params, input_size, args.hidden_size, output_size).to(device)

error = snn.loss(params).to(device)
optimizer = torch.optim.RMSprop(
    net.parameters(), lr=args.lr, weight_decay=0.5
)

train_dataset = ViTacDataset(
    path=args.data_dir, sample_file=f"train_80_20_{args.sample_file}.txt"
)
train_loader = DataLoader(
    dataset=train_dataset, batch_size=args.batch_size, shuffle=False, num_workers=4
)
test_dataset = ViTacDataset(
    path=args.data_dir, sample_file=f"test_80_20_{args.sample_file}.txt"
)
test_loader = DataLoader(
    dataset=test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=4
)

def l1_reg(spike_trains):
    loss = torch.tensor(0.0).to(device)
    for st in spike_trains:
        loss += torch.mean(st)
    return loss

def l2_weight_reg(network):
    l2_reg = torch.tensor(0.)
    for param in network.parameters():
        l2_reg += torch.norm(param)
    return l2_reg

def l2_reg(spike_trains):
    loss = torch.tensor(0.0).to(device)
    for st in spike_trains:
        spikes_per_neuron = (
            torch.sum(st, dim=4).float().reshape((st.shape[0], -1))
        )  # (B, C, H, W ,T)
        l2_per_neuron_spike_loss = 0.5 * torch.mean(spikes_per_neuron ** 2)
        loss += l2_per_neuron_spike_loss
    return loss

def fano_reg(spike_trains):
    loss = torch.tensor(0.0).to(device)
    for st in spike_trains:     # B N 1 1 T
        st = torch.einsum("bnijt->nbijt", st)
        st = st.reshape(st.shape[0], st.shape[1], -1)
        st = st.sum(-1)
        fanos = torch.tensor([N.var() / N.mean() for N in st])
        fanos = fanos[~torch.isnan(fanos)]
        loss += torch.mean(fanos)
    return loss

def fano_var_reg(spike_trains):
    loss = torch.tensor(0.0).to(device)
    for st in spike_trains:     # B N 1 1 T
        st = torch.einsum("bnijt->nbijt", st)
        st = st.reshape(st.shape[0], st.shape[1], -1)
        st = st.sum(-1)
        fano_vars = torch.var(st, dim=1)
        loss += torch.mean(fano_vars)
    return loss

class Losses:
    TOTAL, SPIKE, L1, L2, L2_WEIGHT, FANO, FANO_VAR = range(7)

def _train():
    correct = 0
    num_samples = 0
    losses = [0, 0, 0, 0, 0]
    # losses = [0, 0, 0, 0, 0, 0]
    net.train()
    for i, (tact, _, target, label) in enumerate(train_loader):
        tact = tact.to(device)
        target = target.to(device)
        output = net.forward(tact)
        correct += torch.sum(snn.predict.getClass(output) == label).data.item()
        num_samples += len(label)

        spike_loss = error.numSpikes(output, target)
        l1_loss = l1_reg(net.spike_trains)
        l2_loss = l2_reg(net.spike_trains)
        l2_weight_loss = l2_weight_reg(net)


        loss = spike_loss + args.r_l1 * l1_loss + args.r_l2 * l2_loss

        losses[Losses.L1] += l1_loss
        losses[Losses.L2] += l2_loss
        losses[Losses.SPIKE] += spike_loss

        losses[Losses.L2_WEIGHT] += l2_weight_loss
        losses[Losses.TOTAL] += loss

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    writer.add_scalar("l1_loss/train", losses[Losses.L1] / len(train_loader), epoch)
    writer.add_scalar("l2_loss/train", losses[Losses.L2] / len(train_loader), epoch)
    writer.add_scalar("l2_weight_loss/train", losses[Losses.L2_WEIGHT] / len(train_loader), epoch)
    writer.add_scalar("spike_loss/train", losses[Losses.SPIKE] / len(train_loader), epoch)
    writer.add_scalar("loss/train", losses[Losses.TOTAL] / len(train_loader), epoch)
    writer.add_scalar("acc/train", correct / num_samples, epoch)

    return loss

def _test():
    correct = 0
    num_samples = 0
    losses = [0, 0, 0, 0]
    # losses = [0, 0, 0, 0, 0, 0]
    net.eval()
    with torch.no_grad():
        for i, (tact, _, target, label) in enumerate(test_loader):
            tact = tact.to(device)
            target = target.to(device)
            output = net.forward(tact)
            correct += torch.sum(snn.predict.getClass(output) == label).data.item()
            num_samples += len(label)

            spike_loss = error.numSpikes(output, target)
            l1_loss = l1_reg(net.spike_trains)
            l2_loss = l2_reg(net.spike_trains)
            # fano_loss = fano_reg(net.spike_trains)
            # fano_var_loss = fano_var_reg(net.spike_trains)

            # loss = spike_loss + args.r_l1 * l1_loss + args.r_l2 * l2_loss + args.r_fano * fano_loss + args.r_fano_var * fano_var_loss
            loss = spike_loss + args.r_l1 * l1_loss + args.r_l2 * l2_loss

            losses[Losses.L1] += l1_loss
            losses[Losses.L2] += l2_loss
            losses[Losses.SPIKE] += spike_loss
            # losses[Losses.FANO] += fano_loss
            # losses[Losses.FANO_VAR] += fano_var_loss
            losses[Losses.TOTAL] += loss

        writer.add_scalar("l1_loss/test", losses[Losses.L1] / len(test_loader), epoch)
        writer.add_scalar("l2_loss/test", losses[Losses.L2] / len(test_loader), epoch)
        writer.add_scalar("spike_loss/test", losses[Losses.SPIKE] / len(test_loader), epoch)
        # writer.add_scalar("fano_loss/test", losses[Losses.FANO] / len(test_loader), epoch)
        # writer.add_scalar("fano_var_loss/test", losses[Losses.FANO_VAR] / len(test_loader), epoch)
        writer.add_scalar("loss/test", losses[Losses.TOTAL] / len(test_loader), epoch)
        writer.add_scalar("acc/test", correct / num_samples, epoch)

    return loss


def _save_model(epoch, loss):
    log.info(f"Writing model at epoch {epoch}...")
    checkpoint_path = (
        Path(args.checkpoint_dir) / f"weights-{epoch:03d}-{loss:0.3f}.pt"
    )
    torch.save(net.state_dict(), checkpoint_path)

for epoch in range(1, args.epochs + 1):
    _train()
    if epoch % 10 == 0:
        test_loss = _test()
    if epoch % 100 == 0:
        _save_model(epoch, test_loss)
