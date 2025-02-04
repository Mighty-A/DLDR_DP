import argparse
import os
import shutil
import time

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets

from sklearn.decomposition import PCA
import matplotlib.pyplot as plt
import numpy as np
from numpy import linalg as LA
import pickle
import random
import resnet
from utils import get_datasets, get_model, get_sigma


#package for computing individual gradients
from backpack import backpack, extend
from backpack.extensions import BatchGrad

def set_seed(seed=233): 
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

print ('P-SGD')

parser = argparse.ArgumentParser(description='P(+)-SGD in pytorch')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet32',
                    help='model architecture (default: resnet32)')
parser.add_argument('--datasets', metavar='DATASETS', default='CIFAR10', type=str,
                    help='The training datasets')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=100, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=128, type=int,
                    metavar='N', help='mini-batch size (default: 128)')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=50, type=int,
                    metavar='N', help='print frequency (default: 50)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--half', dest='half', action='store_true',
                    help='use half-precision(16-bit) ')
parser.add_argument('--save-dir', dest='save_dir',
                    help='The directory used to save the trained models',
                    default='save_temp', type=str)
parser.add_argument('--save-every', dest='save_every',
                    help='Saves checkpoints at every specified number of epochs',
                    type=int, default=10)
parser.add_argument('--n_components', default=40, type=int, metavar='N',
                    help='n_components for PCA') 
parser.add_argument('--params_start', default=0, type=int, metavar='N',
                    help='which epoch start for PCA') 
parser.add_argument('--params_end', default=51, type=int, metavar='N',
                    help='which epoch end for PCA') 
parser.add_argument('--alpha', default=0, type=float, metavar='N',
                    help='lr for momentum') 
parser.add_argument('--lr', default=1, type=float, metavar='N',
                    help='lr for PSGD') 
parser.add_argument('--gamma', default=0.9, type=float, metavar='N',
                    help='gamma for momentum')
parser.add_argument('--randomseed', 
                    help='Randomseed for training and initialization',
                    type=int, default=1)

parser.add_argument('--corrupt', default=0, type=float,
                    metavar='c', help='noise level for training set')
parser.add_argument('--smalldatasets', default=None, type=float, dest='smalldatasets', 
                    help='percent of small datasets')

## arguments for learning with differential privacy
parser.add_argument('--eps', default=8., type=float, help='privacy parameter epsilon')
parser.add_argument('--delta', default=1e-5, type=float, help='desired delta')
parser.add_argument('--clip', default=5, type=float, help="clipping the threshold for low dimensional gradient")

args = parser.parse_args()
set_seed(args.randomseed)
best_prec1 = 0
P = None
train_acc, test_acc, train_loss, test_loss = [], [], [], []

def get_model_param_vec(model):
    """
    Return model parameters as a vector
    """
    vec = []
    for name,param in model.named_parameters():
        vec.append(param.detach().cpu().numpy().reshape(-1))
    return np.concatenate(vec, 0)

def get_model_grad_vec(model):
    # Return the model grad as a vector

    vec = []
    for name,param in model.named_parameters():
        vec.append(param.grad.detach().reshape(-1))
    return torch.cat(vec, 0)

def get_model_grad_vec_batch(model):
    # Return the model grad as a vector

    vec = []
    for name,param in model.named_parameters():
        vec.append(param.grad_batch.reshape(param.grad_batch.shape[0], -1))
    return torch.cat(vec, 1)

def update_grad(model, grad_vec):
    idx = 0
    for name,param in model.named_parameters():
        arr_shape = param.grad.shape
        size = 1
        for i in range(len(list(arr_shape))):
            size *= arr_shape[i]
        param.grad.data = grad_vec[idx:idx+size].reshape(arr_shape)
        idx += size

def update_param(model, param_vec):
    idx = 0
    for name,param in model.named_parameters():
        arr_shape = param.data.shape
        size = 1
        for i in range(len(list(arr_shape))):
            size *= arr_shape[i]
        param.data = param_vec[idx:idx+size].reshape(arr_shape)
        idx += size

def main():

    global args, best_prec1, Bk, p0, P

    # Check the save_dir exists or not
    print (args.save_dir)
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)
    
    # Define model
    model = torch.nn.DataParallel(get_model(args))
    model.cuda()
    model = extend(model)

    # Load sampled model parameters
    print ('params: from', args.params_start, 'to', args.params_end)
    W = []
    for i in range(args.params_start, args.params_end):
        ############################################################################
        # if i % 2 != 0: continue

        model.load_state_dict(torch.load(os.path.join(args.save_dir,  str(i) +  '.pt')))
        W.append(get_model_param_vec(model))
    W = np.array(W)
    print ('W:', W.shape)

    # Obtain base variables through PCA
    pca = PCA(n_components=args.n_components)
    pca.fit_transform(W)
    P = np.array(pca.components_)
    print ('ratio:', pca.explained_variance_ratio_)
    print ('P:', P.shape)

    P = torch.from_numpy(P).cuda()

    # Resume from params_start
    model.load_state_dict(torch.load(os.path.join(args.save_dir,  str(args.params_start) +  '.pt')))

    # Prepare Dataloader
    train_loader, val_loader = get_datasets(args)
    
    # Define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss(reduction='sum').cuda()
    criterion = extend(criterion)
    if args.half:
        model.half()
        criterion.half()

    cudnn.benchmark = True

    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
                                                        milestones=[30, 50], last_epoch=args.start_epoch - 1)

    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    # DP
    print('==> Computing noise scale for privacy budget ({:.1f}, {:f})-DP'.format(args.eps, args.delta))
    sampling_prob = 1 / len(train_loader)
    total_steps = int(args.epochs / sampling_prob)
    sigma, eps = get_sigma(sampling_prob, total_steps, args.eps, args.delta, rgp=False)
    noise_multiplier = sigma
    print("noise scale for low-dimensional gradient: ", sigma, "\n privacy guarantee: ", eps)




    

    print ('Train:', (args.start_epoch, args.epochs))
    end = time.time()
    p0 = get_model_param_vec(model)
    for epoch in range(args.start_epoch, args.epochs):
        # Train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, noise_multiplier, args.clip)
        # Bk = torch.eye(args.n_components).cuda()
        lr_scheduler.step()

        # Evaluate on validation set
        prec1 = validate(val_loader, model, criterion)

        # Remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)

    print ('total time:', time.time() - end)
    print ('train loss: ', train_loss)
    print ('train acc: ', train_acc)
    print ('test loss: ', test_loss)
    print ('test acc: ', test_acc)      
    print ('best_prec1:', best_prec1)

    # torch.save(model.state_dict(), 'PBFGS.pt',_use_new_zipfile_serialization=False)  
    torch.save(model.state_dict(), 'PSGD.pt')  

running_grad = 0

def train(train_loader, model, criterion, optimizer, epoch, noise_multiplier, clip):
    # Run one train epoch

    global P, W, iters, T, train_loss, train_acc, search_times, running_grad, p0
    
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # Switch to train mode
    model.train()

    end = time.time()
    for i, (input, target) in enumerate(train_loader):

        # Measure data loading time
        data_time.update(time.time() - end)

        # Load batch data to cuda
        target = target.cuda()
        input_var = input.cuda()
        target_var = target
        if args.half:
            input_var = input_var.half()

        # Compute output
        output = model(input_var)
        loss = criterion(output, target_var)

        # Compute gradient and do SGD step
        optimizer.zero_grad()
        with backpack(BatchGrad()):
            loss.backward()

        # Do P_plus_BFGS update
        gk = get_model_grad_vec_batch(model)
        org_norms_stat, clipped_norms_stat = P_SGD_DP(model, optimizer, gk, loss.item(), input_var, target_var, noise_multiplier=noise_multiplier, clip=clip)

        # Measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))

        # Measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        
        if i % args.print_freq == 0 or i == len(train_loader)-1:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                      epoch, i, len(train_loader), batch_time=batch_time,
                      data_time=data_time, loss=losses, top1=top1))
            print("low dimensional original norms: mean: {:.2f} max: {:.2f}, median: {:2f}".format(org_norms_stat[0], org_norms_stat[1], org_norms_stat[2]))

            print("low dimensional clipped norms: mean: {:.2f} max: {:.2f}, median: {:2f}".format(clipped_norms_stat[0], clipped_norms_stat[1], clipped_norms_stat[2]))


    train_loss.append(losses.avg)
    train_acc.append(top1.avg)


# Set the update period of basis variables (per iterations)
T = 1000

# Set the momentum parameters
gamma = args.gamma
alpha = args.alpha
grad_res_momentum = 0

# Store the last gradient on basis variables for P_plus_BFGS update
gk_last = None

# Variables for BFGS and backtracking line search
rho = 0.55
rho = 0.4
sigma = 0.4
Bk = torch.eye(args.n_components).cuda()
sk = None

# Store the backtracking line search times
search_times = []

def P_SGD(model, optimizer, grad, oldf, X, y):
    # P_plus_BFGS algorithm

    global rho, sigma, Bk, sk, gk_last, grad_res_momentum, gamma, alpha, search_times

    gk = torch.mm(P, grad.reshape(-1,1))

    grad_proj = torch.mm(P.transpose(0, 1), gk)
    grad_res = grad - grad_proj.reshape(-1)

    # Update the model grad and do a step
    update_grad(model, grad_proj)
    optimizer.step()


def clip_column(tsr, clip=1.0, inplace=True):
    if(inplace):
        inplace_clipping(tsr, torch.tensor(clip).cuda())
    else:
        norms = torch.norm(tsr, dim=1)

        scale = torch.clamp(clip/norms, max=1.0)
        return tsr * scale.view(-1, 1) 

@torch.jit.script
def inplace_clipping(matrix, clip):
    n, m = matrix.shape
    for i in range(n):
        # Normalize the i'th row
        col = matrix[i:i+1, :]
        col_norm = torch.sqrt(torch.sum(col ** 2))     
        if(col_norm > clip):
            col /= (col_norm/clip)

def P_SGD_DP(model, optimizer, grad, oldf, X, y, noise_multiplier, clip):
    # P_plus_BFGS algorithm

    global rho, sigma, Bk, sk, gk_last, grad_res_momentum, gamma, alpha, search_times

    selected_bases = P.T
    selected_bases_T = P

    embedding = torch.matmul(grad, selected_bases)


    cur_approx = torch.matmul(torch.mean(embedding, dim=0).view(1, -1), selected_bases_T).view(-1)
    cur_target = torch.mean(grad, dim=0)
    cur_error = torch.sum(torch.square(cur_approx - cur_target)) / torch.sum(torch.square(cur_target))

    embedding_norms = torch.norm(embedding, dim=1)

    org_norms_stat = [torch.mean(embedding_norms).item(), torch.max(embedding_norms).item(), torch.median(embedding_norms).item()]

    # print("approx error: {:.2f}".format(100 * cur_error.item()))

    clipped_embedding = clip_column(embedding, clip=clip, inplace=False)

    clipped_norms = torch.norm(clipped_embedding, dim=1)

    clipped_norms_stat =  [torch.mean(clipped_norms).item(), torch.max(clipped_norms).item(), torch.median(clipped_norms).item()]
    # print('average norm of clipped embedding: ', torch.mean(norms).item(), 'max norm: ', torch.max(norms).item(), 'median norm: ', torch.median(norms).item())

    avg_clipped_embedding = torch.sum(clipped_embedding, dim=0) / embedding.shape[0]

    clipped_theta = avg_clipped_embedding.view(-1)

    theta_noise = torch.normal(0, noise_multiplier * clip / embedding.shape[0], size=clipped_theta.shape, device= clipped_theta.device)

    clipped_theta += theta_noise

    noisy_grad = torch.matmul(clipped_theta.view(1, -1), selected_bases_T).reshape(-1)

    
    # print(noisy_grad.shape)
    # Update the model grad and do a step
    update_grad(model, noisy_grad)
    optimizer.step()

    return org_norms_stat, clipped_norms_stat

def validate(val_loader, model, criterion):
    # Run evaluation

    global test_acc, test_loss  

    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # Switch to evaluate mode
    model.eval()

    end = time.time()
    with torch.no_grad():
        for i, (input, target) in enumerate(val_loader):
            target = target.cuda()
            input_var = input.cuda()
            target_var = target.cuda()

            if args.half:
                input_var = input_var.half()

            # Compute output
            output = model(input_var)
            loss = criterion(output, target_var)

            output = output.float()
            loss = loss.float()

            # Measure accuracy and record loss
            prec1 = accuracy(output.data, target)[0]
            losses.update(loss.item(), input.size(0))
            top1.update(prec1.item(), input.size(0))

            # Measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                          i, len(val_loader), batch_time=batch_time, loss=losses,
                          top1=top1))

    print(' * Prec@1 {top1.avg:.3f}'
          .format(top1=top1))

    # Store the test loss and test accuracy
    test_loss.append(losses.avg)
    test_acc.append(top1.avg)

    return top1.avg

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    # Save the training model

    torch.save(state, filename)

class AverageMeter(object):
    # Computes and stores the average and current value

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, topk=(1,)):
    # Computes the precision@k for the specified values of k

    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

if __name__ == '__main__':
    main()
