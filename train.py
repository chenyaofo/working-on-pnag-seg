import datetime
import os
import time

import torch
import torch.utils.data
from torch import nn
import torchvision

import transforms as T
import dataset

from coco_utils import get_coco
import presets
import utils
from spos_ofa_segmentation import SPOSMobileNetV3Segmentation
from spos_ofa_segmentation.representation import OFAArchitecture

from convert_seg import convert2segmentation
from ofa.ofa_mbv3 import OFAMobileNetV3


def get_dataset(dir_path, name, image_set, transform):
    def sbd(*args, **kwargs):
        return torchvision.datasets.SBDataset(*args, mode='segmentation', **kwargs)

    def get_cityscapes(*args, **kwargs):
        return torchvision.datasets.Cityscapes(*args, mode='fine', target_type="semantic", **kwargs)

    paths = {
        "voc": (dir_path, torchvision.datasets.VOCSegmentation, 21),
        "voc_aug": (dir_path, sbd, 21),
        "coco": (dir_path, get_coco, 21),
        "citys": (dir_path, get_cityscapes, 19)
    }
    p, ds_fn, num_classes = paths[name]

    ds = ds_fn(p, image_set=image_set, transforms=transform)
    return ds, num_classes


def get_transform(train):
    base_size = 520
    crop_size = 480

    return presets.SegmentationPresetTrain(base_size, crop_size) if train else presets.SegmentationPresetEval(base_size)


def criterion(inputs, target):
    losses = {}
    for name, x in inputs.items():
        losses[name] = nn.functional.cross_entropy(x, target, ignore_index=255)

    if len(losses) == 1:
        return losses['out']

    return losses['out'] + 0.5 * losses['aux']


def evaluate(model, data_loader, device, num_classes):
    model.eval()
    confmat = utils.ConfusionMatrix(num_classes)
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    with torch.no_grad():
        for image, target in metric_logger.log_every(data_loader, 100, header):
            image, target = image.to(device), target.to(device)
            output = model(image)
            output = output['out']

            confmat.update(target.flatten(), output.argmax(1).flatten())

        confmat.reduce_from_all_processes()

    return confmat


def train_one_epoch(model, criterion, optimizer, data_loader, lr_scheduler, device, epoch, print_freq):
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value}'))
    header = 'Epoch: [{}]'.format(epoch)
    for image, target in metric_logger.log_every(data_loader, print_freq, header):
        image, target = image.to(device), target.to(device)
        output = model(image)
        loss = criterion(output, target)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        lr_scheduler.step()

        metric_logger.update(loss=loss.item(), lr=optimizer.param_groups[0]["lr"])


def main(args):
    if args.output_dir:
        utils.mkdir(args.output_dir)

    utils.init_distributed_mode(args)
    print(args)

    device = torch.device(args.device)

    # dataset, num_classes = get_dataset(args.data_path, args.dataset, "train", get_transform(train=True))
    # dataset_test, _ = get_dataset(args.data_path, args.dataset, "val", get_transform(train=False))

    value_scale = 255
    mean = [0.485, 0.456, 0.406]
    mean = [item * value_scale for item in mean]
    std = [0.229, 0.224, 0.225]
    std = [item * value_scale for item in std]

    scale_min = 0.5
    scale_max = 1.75
    rotate_min = -1
    rotate_max = 1
    train_h = 512
    train_w = 1024
    ignore_label = 255

    train_transform = T.Compose([
        T.RandScale([scale_min, scale_max]),
        T.RandRotate([rotate_min, rotate_max], padding=mean, ignore_label=ignore_label),
        T.RandomGaussianBlur(),
        T.RandomHorizontalFlip(),
        T.Crop([train_h, train_w], crop_type='rand', padding=mean, ignore_label=ignore_label),
        T.ToTensor(),
        T.Normalize(mean=mean, std=std)
    ])

    dataset_train = dataset.CityscapesData(split='train', data_root=args.data_root, data_list=args.train_list, transform=train_transform)

    val_transform = T.Compose([
        T.Crop([train_h, train_w], crop_type='center', padding=mean, ignore_label=ignore_label),
        T.ToTensor(),
        T.Normalize(mean=mean, std=std)
    ])
    dataset_test = dataset.CityscapesData(split='val', data_root=args.data_root, data_list=args.val_list, transform=val_transform)

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(dataset_train)
        test_sampler = torch.utils.data.distributed.DistributedSampler(dataset_test)
    else:
        train_sampler = torch.utils.data.RandomSampler(dataset_train)
        test_sampler = torch.utils.data.SequentialSampler(dataset_test)

    data_loader = torch.utils.data.DataLoader(
        dataset_train, batch_size=args.batch_size,
        sampler=train_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn, drop_last=True)

    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=1,
        sampler=test_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)
    num_classes = 19
    # model = torchvision.models.segmentation.__dict__[args.model](num_classes=num_classes,
    #                                                              aux_loss=args.aux_loss,
    #                                                              pretrained=args.pretrained)
    if args.pretrained:
        supernet = OFAMobileNetV3(
            n_classes=1000,
            dropout_rate=0,
            width_mult_list=1.2,
            ks_list=[3, 5, 7],
            expand_ratio_list=[3, 4, 6],
            depth_list=[2, 3, 4],
        )
        arch = OFAArchitecture.from_legency_string(args.arch)
        supernet.set_active_subnet(ks=arch.ks, e=arch.ratios, d=arch.depths)
        model = supernet.get_active_subnet()

        s = torch.load("model_best.pth.tar", map_location="cpu")
        model.load_state_dict(s["state_dict_ema"])
        model = convert2segmentation(model=model, begin_index_index=17)
        print("load pretrained model.")
    else:
        supernet = SPOSMobileNetV3Segmentation(width_mult=1.2)
        model = supernet.get_subnet(OFAArchitecture.from_legency_string(args.arch))

    model.to(device)
    if args.distributed:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    if args.pretrained:
        params_to_optimize = [
            {"params": [p for p in model_without_ddp.backbone.parameters() if p.requires_grad]},
            {"params": [p for p in model_without_ddp.stem.parameters() if p.requires_grad]},
        ]
        if args.aux_loss:
            params = [p for p in model_without_ddp.classifier.parameters() if p.requires_grad]
            params_to_optimize.append({"params": params, "lr": args.lr * 10})
        optimizer = torch.optim.SGD(
            params_to_optimize,
            lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    else:
        params_to_optimize = [
            {"params": [p for p in model_without_ddp.first_conv.parameters() if p.requires_grad]},
            {"params": [p for p in model_without_ddp.blocks.parameters() if p.requires_grad]},
            {"params": [p for p in model_without_ddp.remain_block.parameters() if p.requires_grad]},
            {"params": [p for p in model_without_ddp.head.parameters() if p.requires_grad]},
        ]
        if args.aux_loss:
            params = [p for p in model_without_ddp.aux_head.parameters() if p.requires_grad]
            params_to_optimize.append({"params": params, "lr": args.lr})
        optimizer = torch.optim.SGD(
            params_to_optimize,
            lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    lr_scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lambda x: (1 - x / (len(data_loader) * args.epochs)) ** 0.9)

    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'], strict=not args.test_only)
        if not args.test_only:
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1

    if args.test_only:
        confmat = evaluate(model, data_loader_test, device=device, num_classes=num_classes)
        print(confmat)
        return

    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train_one_epoch(model, criterion, optimizer, data_loader, lr_scheduler, device, epoch, args.print_freq)
        confmat = evaluate(model, data_loader_test, device=device, num_classes=num_classes)
        print(confmat)
        utils.save_on_master(
            {
                'model': model_without_ddp.state_dict(),
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': lr_scheduler.state_dict(),
                'epoch': epoch,
                'args': args
            },
            os.path.join(args.output_dir, 'model_{}.pth'.format(epoch)))

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description='PyTorch Segmentation Training')

    parser.add_argument('--arch', default=None, type=str)

    parser.add_argument('--data-root', default='CityScapes', help='dataset path')
    parser.add_argument('--train-list', default='assets/train.lst', help='dataset path')
    parser.add_argument('--val-list', default='assets/val.lst', help='dataset path')
    # parser.add_argument('--dataset', default='citys', help='dataset name')
    # parser.add_argument('--model', default='fcn_resnet101', help='model')
    parser.add_argument('--aux-loss', action='store_true', help='auxiliar loss')
    parser.add_argument('--device', default='cuda', help='device')
    parser.add_argument('-b', '--batch-size', default=2, type=int)
    parser.add_argument('--epochs', default=490, type=int, metavar='N',
                        help='number of total epochs to run')

    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 16)')
    parser.add_argument('--lr', default=0.01, type=float, help='initial learning rate')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                        metavar='W', help='weight decay (default: 1e-4)',
                        dest='weight_decay')
    parser.add_argument('--print-freq', default=10, type=int, help='print frequency')
    parser.add_argument('--output-dir', default='output', help='path where to save')
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    # parser.add_argument('--pretrained', default='', help='resume from checkpoint')
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument(
        "--test-only",
        dest="test_only",
        help="Only test the model",
        action="store_true",
    )
    parser.add_argument(
        "--pretrained",
        dest="pretrained",
        help="Use pre-trained models from the modelzoo",
        action="store_true",
    )
    # distributed training parameters
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist-url', default='env://', help='url used to set up distributed training')

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()
    main(args)
