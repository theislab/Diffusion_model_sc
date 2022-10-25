import os
import argparse
import torch
import torch.nn as nn
import pickle
import numpy as np
import random
from tqdm import tqdm
from torch.utils.data import TensorDataset, DataLoader
import matplotlib.pyplot as plt
import pandas as pd

pd.set_option('display.max_rows', None)

parser = argparse.ArgumentParser(description='Classifier on Representations')
parser.add_argument('--data', type=str, default='cifar10',
                    help='path to dataset')
parser.add_argument('--mode', type=str, default='probabilistic-drl',
                    help='Model mode')
parser.add_argument('--aug', type=str, default='none',
                    help='Aug mode')
parser.add_argument('--widen-factor', default=2, type=int, metavar='N',
                    help='widen factor for WReN')
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--batch-size', type=int, default=256)
parser.add_argument('--epochs', type=int, default=100)
parser.add_argument('--granularity', type=int, default=None)
parser.add_argument('--score-lr', type=str, default='2e-4')
args = parser.parse_args()

args.seed += int(os.environ['SLURM_PROCID'])
k = args.granularity
if args.granularity is None:
    ext_name = ''
else:
    ext_name = f'_{args.granularity}'

if k == 1:
    times = [0.5]
else:
    times = (np.arange(k + 1) / (k))
seeds = 3
lrs = [0.001, 0.00075, 0.0005, 0.00025, 0.0001, 0.00005]

if args.data == 'cifar10':
    num_classes = 10
else:
    num_classes = 100

if args.widen_factor == 2:
    in_dim = 128
else:
    in_dim = 256

def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)

device = torch.device('cuda:0')

class Model(nn.Module):
    def __init__(self, in_dim, num_classes):
        super(Model, self).__init__()

        self.classifier = nn.Sequential(
            nn.Linear(in_dim, 512),
            nn.ReLU(),
            nn.Dropout(0.25),
            nn.Linear(512, num_classes)
        )

    def forward(self, x):
        return self.classifier(x)

def train(epoch, loader, time):
    model.train()
    total_loss = 0.
    total_acc = 0.
    total = 0.
    with tqdm(loader, unit="batch", ncols=100) as tepoch:
        for data, target in tepoch:
            tepoch.set_description(f"Train Epoch {epoch}")
            data, target = data.to(device), target.long().to(device)
            optimizer.zero_grad()
            output = model(data)
            predictions = output.argmax(dim=1, keepdim=True).squeeze()
            loss = criterion(output, target)
            correct = (predictions == target).sum().item()

            total_loss += loss.item()
            total_acc += correct
            total += data.shape[0]

            loss.backward()
            optimizer.step()

            tepoch.set_postfix(loss=total_loss / total, accuracy=100. * (total_acc / total), time=time)

    return 100. * (total_acc / total)

def eval(epoch, loader):
    model.eval()
    total_loss = 0.
    total_acc = 0.
    total = 0.

    with torch.no_grad():
        with tqdm(loader, unit="batch", ncols=100) as tepoch:
            for data, target in tepoch:
                tepoch.set_description(f"Test Epoch {epoch}")
                data, target = data.to(device), target.long().to(device)
                output = model(data)
                predictions = output.argmax(dim=1, keepdim=True).squeeze()
                loss = criterion(output, target)
                correct = (predictions == target).sum().item()

                total_loss += loss.item()
                total_acc += correct
                total += data.shape[0]

                tepoch.set_postfix(loss=total_loss / total, accuracy=100. * (total_acc / total))

    return 100. * (total_acc / total)

nm = f'../diffusion_model/trained_models/{args.score_lr}/{args.mode}/{args.data}_{args.widen_factor}_{args.aug}_{args.seed}'

if os.path.exists(f'{nm}/eval_log_opt{ext_name}.txt'):
    print('Evaluation Already Done')
    exit()

if args.granularity == 1:
    train_name = f'{nm}/train_2.pkl'
    test_name = f'{nm}/test_2.pkl'
else:
    train_name = f'{nm}/train{ext_name}.pkl'
    test_name = f'{nm}/test{ext_name}.pkl'

with open(train_name, 'rb') as f:
    train_data = pickle.load(f)

with open(test_name, 'rb') as f:
    test_data = pickle.load(f)

vals = np.zeros([len(lrs), len(times), seeds])
perf_trains = np.zeros([len(lrs), len(times), seeds])
perf_tests = np.zeros([len(lrs), len(times), seeds])

for seed in range(seeds):
    set_seed(seed)
    idx = np.arange(50000)
    np.random.shuffle(idx)

    train_idx = idx[:45000]
    val_idx = idx[45000:]

    for j,time in enumerate(times):
        X_train = torch.Tensor(train_data['Representation'][time])
        X_test = torch.Tensor(test_data['Representation'][time])

        y_train = torch.Tensor(train_data['Labels'])
        y_test = torch.Tensor(test_data['Labels'])

        X_val = X_train[val_idx]
        y_val = y_train[val_idx]

        X_train = X_train[train_idx]
        y_train = y_train[train_idx]

        train_dataset = TensorDataset(X_train, y_train)
        del X_train, y_train
        train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, drop_last=True)

        val_dataset = TensorDataset(X_val, y_val)
        del X_val, y_val
        val_dataloader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, drop_last=False)

        test_dataset = TensorDataset(X_test, y_test)
        del X_test, y_test
        test_dataloader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, drop_last=False)

        for i, lr in enumerate(lrs):
            model = Model(in_dim, num_classes).to(device)
            num_params = sum(p.numel() for p in model.parameters())
            if seed == 0 and j == 0 and i == 0:
                print(model)
                print(f"Number of Parameters: {num_params}")

            optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
            criterion = nn.CrossEntropyLoss()
            best_val = 0.
            opt_test = 0.

            for epoch in range(1, args.epochs + 1):
                train_acc = train(epoch, train_dataloader, time)
                val_acc = eval(epoch, val_dataloader)
                test_acc = eval(epoch, test_dataloader)

                if val_acc > best_val:
                    best_val = val_acc
                    opt_test = test_acc

            vals[i, j, seed] = best_val
            perf_trains[i, j, seed] = train_acc
            perf_tests[i, j, seed] = opt_test

vals = np.mean(vals, axis=-1)
perf_train = np.mean(perf_trains, axis=-1)
perf_test = np.mean(perf_tests, axis=-1)

idxs = []
for i, _ in enumerate(times):
    idx = np.argmax(vals[:,i])
    idxs.append(idx)

lr = lrs[idx]

log = 'Pretrained:\n'
for i, tm in enumerate(times):
    lr = lrs[idxs[i]]
    log += f'Time: {tm} | Learning Rate: {lr} | Train Accuracy: {perf_train[idxs[i], i]} | Test Accuracy: {perf_test[idxs[i], i]}\n'

with open(f'{nm}/eval_log_opt{ext_name}.txt', 'w') as f:
    f.write(log)
print(log)
