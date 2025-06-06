import os
import sys

os.environ["CUDA_VISIBLE_DEVICES"] = "0"
sys.path.append(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
import time
import argparse
import copy
from tqdm import tqdm
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from easydict import EasyDict
import unfoldNd
import torch
from torch.utils.data import DataLoader, ConcatDataset, Subset, random_split
import torch.optim as optim
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter
from torchvision.transforms import ToPILImage
from data.dataset_3d import LADataset
from models import deeplabv3
from utils.loss_functions import DSCLoss
from utils.logger import logger as logging
from utils.utils import *
from utils.mask_generator import BoxMaskGenerator, AddMaskParamsToBatch, SegCollate
from utils.ramps import sigmoid_rampup
from utils.torch_utils import seed_torch
from utils.model_init import init_weight

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def get_args(known=False):
    parser = argparse.ArgumentParser(description='PyTorch Implementation')
    parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed (default: 1)')
    parser.add_argument('--project', type=str,
                        default=os.path.dirname(os.path.realpath(__file__)) + '',
                        help='project path for saving results')
    parser.add_argument('--backbone', type=str, default='VNet', choices=['VNet'], help='segmentation backbone')
    parser.add_argument('--data_path', type=str, default='dataset/LA/', help='path to the data')
    parser.add_argument('--image_size', type=int, default=[80, 112, 112],
                        help='the size of images for training and testing')
    parser.add_argument('--labeled_percentage', type=float, default=0.1, help='the percentage of labeled data')
    parser.add_argument('--is_mix', type=bool, default=True, help='cut mix')
    parser.add_argument('--topk', type=int, default=2, help='top k')
    parser.add_argument('--num_epochs', type=int, default=500, help='number of epochs')
    parser.add_argument('--batch_size', type=int, default=4, help='number of inputs per batch')
    parser.add_argument('--num_workers', type=int, default=2, help='number of workers to use for dataloader')
    parser.add_argument('--in_channels', type=int, default=1, help='input channels')
    parser.add_argument('--num_classes', type=int, default=2, help='number of target categories')
    parser.add_argument('--learning_rate', type=float, default=1e-4, help='learning rate')
    parser.add_argument('--intra_weights', type=list, default=[1., 1.],
                        help='inter classes weighted coefficients in the loss function')
    parser.add_argument('--inter_weight', type=float, default=1,
                        help='inter losses weighted coefficients in the loss function')
    parser.add_argument('--log_freq', type=float, default=1,
                        help='logging frequency of metrics accord to the current iteration')
    parser.add_argument('--save_freq', type=float, default=10,
                        help='saving frequency of model weights accord to the current epoch')
    args = parser.parse_known_args()[0] if known else parser.parse_args()
    return args


def get_data(args):
    val_set = LADataset(image_path=args.data_path, stage='val', image_size=args.image_size, is_augmentation=False)
    train_set = LADataset(image_path=args.data_path, stage='train', image_size=args.image_size, is_augmentation=True)
    labeled_train_set, unlabeled_train_set = random_split(train_set, [int(len(train_set) * args.labeled_percentage),
                                                                      len(train_set) - int(
                                                                          len(train_set) * args.labeled_percentage)],
                                                          generator=torch.Generator().manual_seed(args.seed))

    # repeat the labeled set to have a equal length with the unlabeled set (dataset)
    print('before: ', len(train_set), len(labeled_train_set), len(val_set))
    labeled_ratio = len(train_set) // len(labeled_train_set)
    labeled_train_set = ConcatDataset([labeled_train_set for i in range(labeled_ratio)])
    labeled_train_set = ConcatDataset([labeled_train_set,
                                       Subset(labeled_train_set, range(len(train_set) - len(labeled_train_set)))])
    print('after: ', len(train_set), len(labeled_train_set), len(val_set))
    assert len(labeled_train_set) == len(train_set)
    train_labeled_dataloder = DataLoader(dataset=labeled_train_set, num_workers=args.num_workers,
                                         batch_size=args.batch_size, shuffle=True, pin_memory=True)
    train_unlabeled_dataloder = DataLoader(dataset=train_set, num_workers=args.num_workers, batch_size=args.batch_size,
                                           shuffle=True, pin_memory=True)
    val_dataloder = DataLoader(dataset=val_set, num_workers=args.num_workers, batch_size=args.batch_size, shuffle=False,
                               pin_memory=True)
    mask_generator = BoxMaskGenerator(prop_range=(0.25, 0.5),
                                      n_boxes=3,
                                      random_aspect_ratio=True,
                                      prop_by_area=True,
                                      within_bounds=True,
                                      invert=True)

    add_mask_params_to_batch = AddMaskParamsToBatch(mask_generator)
    mask_collate_fn = SegCollate(batch_aug_fn=add_mask_params_to_batch)
    aux_dataloder = DataLoader(dataset=train_set, num_workers=args.num_workers, batch_size=args.batch_size,
                               shuffle=True, pin_memory=True, collate_fn=mask_collate_fn)
    return train_labeled_dataloder, train_unlabeled_dataloder, val_dataloder, aux_dataloder


Good_student = 0


def main(is_debug=False):
    args = get_args()
    seed_torch(args.seed)
    # Project Saving Path
    project_path = args.project + '_{}_label_{}/'.format(args.backbone, args.labeled_percentage)
    ensure_dir(project_path)
    save_path = project_path + 'weights/'
    ensure_dir(save_path)

    # Tensorboard & Statistics Results & Logger
    tb_dir = project_path + '/tensorboard{}'.format(time.strftime("%b%d_%d-%H-%M", time.localtime()))
    writer = SummaryWriter(tb_dir)
    metrics = EasyDict()
    metrics.train_total_s1_loss = []
    metrics.train_total_s2_loss = []
    metrics.train_s_loss = []
    metrics.train_u_loss = []
    metrics.train_x_loss = []
    metrics.val_loss = []
    logger = logging(project_path + 'train_val.log')
    logger.info('PyTorch Version {}\n Experiment{}'.format(torch.__version__, project_path))

    # Load Data
    train_labeled_dataloader, train_unlabeled_dataloader, val_dataloader, aux_loader = get_data(args=args)
    iters = len(train_labeled_dataloader)
    val_iters = len(val_dataloader)

    # Load Model & EMA
    student1 = deeplabv3.__dict__[args.backbone](in_channels=args.in_channels, out_channels=args.num_classes).to(device)
    init_weight(student1.net.classifier, nn.init.kaiming_normal_,
                nn.BatchNorm3d, 1e-5, 0.1,
                mode='fan_in', nonlinearity='relu')

    student2 = deeplabv3.__dict__[args.backbone](in_channels=args.in_channels, out_channels=args.num_classes).to(device)
    init_weight(student2.net.classifier, nn.init.kaiming_normal_,
                nn.BatchNorm3d, 1e-5, 0.1,
                mode='fan_in', nonlinearity='relu')

    teacher = deeplabv3.__dict__[args.backbone](in_channels=args.in_channels, out_channels=args.num_classes).to(device)
    init_weight(teacher.net.classifier, nn.init.kaiming_normal_,
                nn.BatchNorm3d, 1e-5, 0.1,
                mode='fan_in', nonlinearity='relu')

    teacher.detach_model()

    d, h, w = args.image_size[0] // 8, args.image_size[1] // 8, args.image_size[2] // 8
    unfolds = unfoldNd.UnfoldNd(kernel_size=(d, h, w), stride=(d, h, w)).to(device)
    folds = unfoldNd.FoldNd(output_size=(args.image_size[0], args.image_size[1], args.image_size[2]),
                            kernel_size=(d, h, w), stride=(d, h, w)).to(device)
    best_epoch = 0
    best_loss = 100

    # Criterion & Optimizer & LR Schedule
    criterion_dsc = DSCLoss(num_classes=args.num_classes, intra_weights=args.intra_weights,
                            inter_weight=args.inter_weight, device=device, is_3d=True)
    optimizer1 = optim.AdamW(student1.parameters(), lr=args.learning_rate, betas=(0.9, 0.999))
    optimizer2 = optim.AdamW(student2.parameters(), lr=args.learning_rate, betas=(0.9, 0.999))

    # Train
    since = time.time()
    logger.info('start training')
    for epoch in range(1, args.num_epochs + 1):
        epoch_metrics = EasyDict()
        epoch_metrics.train_total_s1_loss = []
        epoch_metrics.train_total_s2_loss = []
        epoch_metrics.train_s_loss = []
        epoch_metrics.train_u_loss = []
        epoch_metrics.train_x_loss = []
        if is_debug:
            pbar = range(10)
        else:
            pbar = range(iters)
        iter_train_labeled_dataloader = iter(train_labeled_dataloader)
        iter_train_unlabeled_dataloader = iter(train_unlabeled_dataloader)
        iter_aux_loader = iter(aux_loader)

        student1.train()
        student2.train()
        teacher.train()

        winner_s = 0
        for idx in pbar:
            # label data
            image, label, imageA1, imageA2 = next(iter_train_labeled_dataloader)
            image, label = image.to(device), label.to(device)
            imageA1, imageA2 = imageA1.to(device), imageA2.to(device)
            # unlabel data
            uimage, _, uimageA1, uimageA2 = next(iter_train_unlabeled_dataloader)
            uimage, uimageA1, uimageA2 = uimage.to(device), uimageA1.to(device), uimageA2.to(device)
            # auxiliary data
            aimage, alabel, aimageA1, aimageA2, amask = next(iter_aux_loader)
            aimage, alabel = aimage.to(device), alabel.to(device)
            aimageA1, aimageA2, amask = aimageA1.to(device), aimageA2.to(device), amask.to(device).long()

            optimizer1.zero_grad()
            optimizer2.zero_grad()

            # the supervised learning path #
            with torch.no_grad():
                pred = teacher(image)
                pred_t_logits = pred['out']
                pred_t_probs = torch.softmax(pred_t_logits, dim=1)  # 8 4 256 256
                pred_u = teacher(uimage)
                pred_u_logits = pred_u['out']
                pred_u_probs = torch.softmax(pred_u_logits, dim=1)  # 8 4 256 256
                pred_u_pseudo = torch.argmax(pred_u_probs, dim=1)  # 8 256 256
                pred_u_conf = pred_u_probs.max(dim=1)[0].clone()

            pred_s1_logits = student1(imageA1)['out']
            pred_s1_probs = torch.softmax(pred_s1_logits, dim=1)  # 8 4 256 256
            pred_s2_logits = student2(imageA2)['out']
            pred_s2_probs = torch.softmax(pred_s2_logits, dim=1)  # 8 4 256 256

            pred_u1A1 = student1(uimageA1)
            pred_u1A1_logits = pred_u1A1['out']
            pred_u1A1_probs = torch.softmax(pred_u1A1_logits, dim=1)  # 8 4 256 256
            pred_u1A1_pseudo = torch.argmax(pred_u1A1_probs, dim=1)  # 8 256 256
            pred_u1A1_conf = pred_u1A1_probs.max(dim=1)[0].clone()

            pred_u2A2 = student2(uimageA2)
            pred_u2A2_logits = pred_u2A2['out']
            pred_u2A2_probs = torch.softmax(pred_u2A2_logits, dim=1)  # 8 4 256 256
            pred_u2A2_pseudo = torch.argmax(pred_u2A2_probs, dim=1)  # 8 256 256
            pred_u2A2_conf = pred_u2A2_probs.max(dim=1)[0].clone()

            loss_s1 = criterion_dsc(pred_s1_logits, label.squeeze(1).long())
            loss_s2 = criterion_dsc(pred_s2_logits, label.squeeze(1).long())
            loss_s = (loss_s1 + loss_s2) / 2.

            lambda_ = sigmoid_rampup(current=idx + len(pbar) * (epoch - 1), rampup_length=len(pbar) * 5)

            loss_x = (criterion_dsc(pred_u1A1_logits, pred_u2A2_pseudo.detach()) + criterion_dsc(pred_u2A2_logits,
                                                                                                 pred_u1A1_pseudo.detach())) / 2.

            if loss_s1 < loss_s2:
                Good_student = 0

            else:
                Good_student = 1

            if Good_student == 0:
                loss_u = criterion_dsc(pred_u2A2_logits, pred_u_pseudo.detach())

                loss_total_s1 = loss_s + loss_x * 0.1 * lambda_
                loss_total_s2 = loss_s + loss_x * 0.1 * lambda_ + loss_u * 0.1 * lambda_

                loss_total_s1.backward(retain_graph=True)
                loss_total_s2.backward()
                optimizer1.step()
                optimizer2.step()
                teacher.ema_update(student1, ema_decay=0.99, cur_step=idx + len(pbar) * (epoch - 1))

                writer.add_scalar('train_u_loss', loss_u.item(), idx + len(pbar) * (epoch - 1))
                writer.add_scalar('train_total_s1_loss', loss_u.item(), idx + len(pbar) * (epoch - 1))
                writer.add_scalar('train_total_s2_loss', loss_u.item(), idx + len(pbar) * (epoch - 1))

            if Good_student == 1:
                loss_u = criterion_dsc(pred_u1A1_logits, pred_u_pseudo.detach())

                loss_total_s1 = loss_s + loss_x * 0.1 * lambda_ + loss_u * 0.1 * lambda_
                loss_total_s2 = loss_s + loss_x * 0.1 * lambda_

                loss_total_s1.backward(retain_graph=True)
                loss_total_s2.backward()
                optimizer1.step()
                optimizer2.step()
                teacher.ema_update(student2, ema_decay=0.99, cur_step=idx + len(pbar) * (epoch - 1))

                writer.add_scalar('train_u_loss', loss_u.item(), idx + len(pbar) * (epoch - 1))
                writer.add_scalar('train_total_s1_loss', loss_u.item(), idx + len(pbar) * (epoch - 1))
                writer.add_scalar('train_total_s2_loss', loss_u.item(), idx + len(pbar) * (epoch - 1))

            writer.add_scalar('train_s_loss', loss_s.item(), idx + len(pbar) * (epoch - 1))
            # writer.add_scalar('train_u_loss', loss_u.item(), idx + len(pbar) * (epoch-1))
            writer.add_scalar('train_x_loss', loss_x.item(), idx + len(pbar) * (epoch - 1))
            # writer.add_scalar('train_loss', loss.item(), idx + len(pbar) * (epoch-1))
            if idx % args.log_freq == 0:
                logger.info("Train: Epoch/Epochs {}/{}, "
                            "iter/iters {}/{}, "
                            "loss_total_s1 {:.3f},loss_total_s2 {:.3f}, loss_s {:.3f}, loss_u {:.3f}, loss_x {:.3f}, lambda {:.3f}".format(
                    epoch, args.num_epochs, idx, len(pbar),
                    loss_total_s1.item(), loss_total_s2.item(), loss_s.item(), loss_u.item(), loss_x.item(), lambda_))
            epoch_metrics.train_total_s1_loss.append(loss_total_s1.item())
            epoch_metrics.train_total_s2_loss.append(loss_total_s2.item())
            epoch_metrics.train_s_loss.append(loss_s.item())
            epoch_metrics.train_u_loss.append(loss_u.item())
            epoch_metrics.train_x_loss.append(loss_x.item())

        metrics.train_total_s1_loss.append(np.mean(epoch_metrics.train_total_s1_loss))
        metrics.train_total_s2_loss.append(np.mean(epoch_metrics.train_total_s2_loss))
        metrics.train_s_loss.append(np.mean(epoch_metrics.train_s_loss))
        metrics.train_u_loss.append(np.mean(epoch_metrics.train_u_loss))
        metrics.train_x_loss.append(np.mean(epoch_metrics.train_x_loss))

        # Validation
        epoch_metrics.val_loss = []
        iter_val_dataloader = iter(val_dataloader)
        if is_debug:
            val_pbar = range(10)
        else:
            val_pbar = range(val_iters)
        teacher.eval()
        student1.eval()
        student2.eval()

        with torch.no_grad():
            for idx in val_pbar:
                image, label = next(iter_val_dataloader)
                image, label = image.to(device), label.to(device)
                pred = teacher(image)['out']
                pred_s1 = student1(image)['out']
                pred_s2 = student2(image)['out']
                loss = criterion_dsc(pred, label.squeeze(1).long())

                writer.add_scalar('train_loss_sup', loss.item(), idx + len(val_pbar) * (epoch - 1))
                if idx % args.log_freq == 0:
                    logger.info("Val: Epoch/Epochs {}/{}, "
                                "iter/iters {}/{}, "
                                "loss {:.3f}".format(epoch, args.num_epochs, idx, len(val_pbar),
                                                     loss.item()))
                epoch_metrics.val_loss.append(loss.item())
        logger.info("Average: Epoch/Epoches {}/{}, "
                    "train s1 epoch loss {:.3f}, "
                    "train s2 epoch loss {:.3f},"
                    "val epoch loss {:.3f}\n ".format(epoch, args.num_epochs,
                                                      np.mean(epoch_metrics.train_total_s1_loss),
                                                      np.mean(epoch_metrics.train_total_s2_loss),
                                                      np.mean(epoch_metrics.val_loss)))
        metrics.val_loss.append(np.mean(epoch_metrics.val_loss))

        # Save Model
        if np.mean(epoch_metrics.val_loss) <= best_loss:
            best_epoch = epoch
            best_loss = np.mean(epoch_metrics.val_loss)
            torch.save(teacher.state_dict(), save_path + 'best.pth'.format(best_epoch))
        torch.save(teacher.state_dict(), save_path + 'last.pth'.format(best_epoch))

    ############################
    # Save Metrics
    ############################

    data_frame = pd.DataFrame(
        data={'loss_total_s1': metrics.train_total_s1_loss,
              'loss_total_s2': metrics.train_total_s2_loss,
              'loss_s': metrics.train_s_loss,
              'loss_u': metrics.train_u_loss,
              'loss_x': metrics.train_x_loss,
              'val_loss': metrics.val_loss},
        index=range(1, args.num_epochs + 1))
    data_frame.to_csv(project_path + 'train_val_loss.csv', index_label='Epoch')

    time_elapsed = time.time() - since
    logger.info('Training completed in {:.0f}m {:.0f}s'.format(
        time_elapsed // 60, time_elapsed % 60))
    logger.info('TRAINING FINISHED!')


if __name__ == '__main__':
    main()
