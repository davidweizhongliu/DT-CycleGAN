#!/usr/bin/python3

import argparse
import itertools
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
from torch.autograd import Variable
from PIL import Image
import torch
from torch.utils.tensorboard import SummaryWriter

from models import Generator
from models import Discriminator
from utils import ReplayBuffer
from utils import ReplayMemory
from utils import LambdaLR
from utils import weights_init_normal
from datasets import ImageDataset
from model import DetModel
if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--epoch', type=int, default=0, help='starting epoch')
    parser.add_argument('--n_epochs', type=int, default=20, help='number of epochs of training')
    parser.add_argument('--batchSize', type=int, default=1, help='size of the batches')
    parser.add_argument('--dataroot', type=str, default='./Data/', help='root directory of the dataset')
    parser.add_argument('--lr', type=float, default=0.0002, help='initial learning rate')
    parser.add_argument('--decay_epoch', type=int, default=10, help='epoch to start linearly decaying the learning rate to 0')
    parser.add_argument('--input_nc', type=int, default=6, help='number of channels of input data')
    parser.add_argument('--output_nc', type=int, default=6, help='number of channels of output data')
    parser.add_argument('--cuda', action='store_true', help='use GPU computation')
    parser.add_argument('--n_cpu', type=int, default=1, help='number of cpu threads to use during batch generation')
    opt = parser.parse_args()
    opt.size = (180,320)
    print(opt)

    if torch.cuda.is_available() and not opt.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

    ###### Definition of variables ######
    # Networks
    netG_A2B = Generator(opt.input_nc, opt.output_nc)
    netG_B2A = Generator(opt.output_nc, opt.input_nc)
    netD_A = Discriminator(opt.input_nc)
    netD_B = Discriminator(opt.output_nc)
    netDet = DetModel(4,'swin_tiny_patch4_window7_224')
    # netDet.load_state_dict(torch.load('./output/ObjectDet_Swin_Ti_12k', map_location=torch.device('cpu'))['model'])
    netG_A2B.load_state_dict(torch.load('./output/netG_A2B_withdet_9.pth', map_location=torch.device('cpu')))
    netG_B2A.load_state_dict(torch.load('./output/netG_B2A_withdet_9.pth', map_location=torch.device('cpu')))
    netD_B.load_state_dict(torch.load('./output/netD_B_withdet_9.pth', map_location=torch.device('cpu')))
    netD_A.load_state_dict(torch.load('./output/netD_A_withdet_9.pth', map_location=torch.device('cpu')))
    if opt.cuda:
        netG_A2B.cuda()
        netG_B2A.cuda()
        netD_A.cuda()
        netD_B.cuda()
        netDet.cuda()

    # netG_A2B.apply(weights_init_normal)
    # netG_B2A.apply(weights_init_normal)
    # netD_A.apply(weights_init_normal)
    # netD_B.apply(weights_init_normal)

    # Lossess
    criterion_GAN = torch.nn.MSELoss()
    criterion_cycle = torch.nn.L1Loss()
    criterion_identity = torch.nn.L1Loss()
    criterion_consistency = torch.nn.L1Loss()
    criterion_operation = torch.nn.L1Loss()

    # Optimizers & LR schedulers
    optimizer_G = torch.optim.Adam(itertools.chain(netG_A2B.parameters(), netG_B2A.parameters()),
                                    lr=opt.lr, betas=(0.5, 0.999))
    optimizer_D_A = torch.optim.Adam(netD_A.parameters(), lr=opt.lr, betas=(0.5, 0.999))
    optimizer_D_B = torch.optim.Adam(netD_B.parameters(), lr=opt.lr, betas=(0.5, 0.999))
    optimizer_Det = torch.optim.SGD(netDet.parameters(), lr=opt.lr)

    lr_scheduler_G = torch.optim.lr_scheduler.LambdaLR(optimizer_G, lr_lambda=LambdaLR(opt.n_epochs, opt.epoch, opt.decay_epoch).step)
    lr_scheduler_D_A = torch.optim.lr_scheduler.LambdaLR(optimizer_D_A, lr_lambda=LambdaLR(opt.n_epochs, opt.epoch, opt.decay_epoch).step)
    lr_scheduler_D_B = torch.optim.lr_scheduler.LambdaLR(optimizer_D_B, lr_lambda=LambdaLR(opt.n_epochs, opt.epoch, opt.decay_epoch).step)
    lr_scheduler_Det = torch.optim.lr_scheduler.LambdaLR(optimizer_Det, lr_lambda=LambdaLR(opt.n_epochs, opt.epoch, opt.decay_epoch).step)

    # Inputs & targets memory allocation
    Tensor = torch.cuda.FloatTensor if opt.cuda else torch.Tensor
    input_A = Tensor(opt.batchSize, opt.input_nc, opt.size[0], opt.size[1])
    input_B = Tensor(opt.batchSize, opt.output_nc, opt.size[0], opt.size[1])
    target_real = Variable(Tensor(opt.batchSize).fill_(1.0), requires_grad=False)
    target_fake = Variable(Tensor(opt.batchSize).fill_(0.0), requires_grad=False)

    fake_A_buffer = ReplayBuffer()
    fake_B_buffer = ReplayBuffer()

    # Dataset loader
    transforms_ = [transforms.ToTensor()]
    transforms_det = transforms.Resize((224,224))
    dataloader = DataLoader(ImageDataset(opt.dataroot, transforms_=transforms_, unaligned=True, rate=1.0),
                            batch_size=opt.batchSize, shuffle=True, num_workers=opt.n_cpu)
    print('Dataset Length: ', len(dataloader))
    # Loss plot
    writer = SummaryWriter()
    ###################################

    ###### Training ######
    step = 0
    for epoch in range(opt.epoch, opt.n_epochs):
        for i, batch in enumerate(dataloader):
            step += 1
            # Set model input
            real_A = Variable(input_A.copy_(batch['A']))
            real_B = Variable(input_B.copy_(batch['B']))
            label = Tensor(batch['B_label'])

            ###### Generators A2B and B2A ######

            # Identity loss
            # G_A2B(B) should equal B if real B is fed
            same_B = netG_A2B(real_B) # B -> B
            loss_identity_B = criterion_identity(same_B, real_B)*2.0
            # G_B2A(A) should equal A if real A is fed
            same_A = netG_B2A(real_A) # A -> A
            loss_identity_A = criterion_identity(same_A, real_A)*2.0

            # GAN loss
            fake_B = netG_A2B(real_A) # A -> B
            pred_fake = netD_B(fake_B)
            loss_GAN_A2B = criterion_GAN(pred_fake, target_real)

            fake_A = netG_B2A(real_B) # B -> A
            pred_fake = netD_A(fake_A)
            loss_GAN_B2A = criterion_GAN(pred_fake, target_real)

            y_real = netDet(transforms_det(real_B))
            y_fake = netDet(transforms_det(fake_A))
            z_real = netDet(transforms_det(real_A))
            z_fake = netDet(transforms_det(fake_B))
            loss_consist = criterion_consistency(y_fake, y_real) * 10.0 + criterion_consistency(z_fake, z_real) * 10.0


            # Cycle loss
            recovered_A = netG_B2A(fake_B)
            loss_cycle_ABA = criterion_cycle(recovered_A, real_A)*5.0

            recovered_B = netG_A2B(fake_A)
            loss_cycle_BAB = criterion_cycle(recovered_B, real_B)*5.0

            # Total loss
            loss_G = loss_identity_A + loss_identity_B + loss_GAN_A2B + loss_GAN_B2A + loss_cycle_ABA + loss_cycle_BAB + loss_consist
            optimizer_G.zero_grad()
            loss_G.backward()
            optimizer_G.step()
            ###################################
            
            y_real = netDet(transforms_det(real_B))
            y_fake = netDet(transforms_det(fake_A.detach()))
            loss_det = criterion_operation(y_fake,label)*5 +criterion_operation(y_real,label)*5
            optimizer_Det.zero_grad()
            loss_det.backward()
            optimizer_Det.step()

            ###### Discriminator A ######

            # Real loss
            pred_real = netD_A(real_A)
            loss_D_real = criterion_GAN(pred_real, target_real)

            # Fake loss
            fake_A = fake_A_buffer.push_and_pop(fake_A)
            pred_fake = netD_A(fake_A.detach())
            loss_D_fake = criterion_GAN(pred_fake, target_fake)

            # Total loss
            loss_D_A = (loss_D_real + loss_D_fake)*0.5

            optimizer_D_A.zero_grad()
            loss_D_A.backward()

            optimizer_D_A.step()
            ###################################

            ###### Discriminator B ######

            # Real loss
            pred_real = netD_B(real_B)
            loss_D_real = criterion_GAN(pred_real, target_real)

            # Fake loss
            fake_B = fake_B_buffer.push_and_pop(fake_B)
            pred_fake = netD_B(fake_B.detach())
            loss_D_fake = criterion_GAN(pred_fake, target_fake)

            # Total loss
            loss_D_B = (loss_D_real + loss_D_fake)*0.5
            optimizer_D_B.zero_grad()
            loss_D_B.backward()

            optimizer_D_B.step()
            ###################################

            writer.add_scalar('Loss_G',loss_G,step)
            writer.add_scalar('Loss_G_identity',loss_identity_A + loss_identity_B,step)
            writer.add_scalar('Loss_G_GAN',loss_GAN_A2B + loss_GAN_B2A,step)
            writer.add_scalar('Loss_G_cycle',loss_cycle_ABA + loss_cycle_BAB,step)
            writer.add_scalar('Loss_D',loss_D_A + loss_D_B,step)
            writer.add_scalar('Loss_G_consistent',loss_consist,step)
            writer.add_scalar('Loss_Det',loss_det,step)
            #writer.add_image('Real/local',  real_A[0,:3, :, :])
            #writer.add_image('Real/global', real_A[0,3:, :, :])
            #writer.add_image('To_real/local',  fake_A[0, :3, :, :])
            #writer.add_image('To_real/global', fake_A[0, 3:, :, :])
            #writer.add_image('Simu/local',  real_B[0, :3, :, :])
            #writer.add_image('Simu/global', real_B[0, 3:, :, :])
            #writer.add_image('To_simu/local',  fake_B[0, :3,:,:])
            #writer.add_image('To_simu/global', fake_B[0, 3:, :, :])

        # Update learning rates
        lr_scheduler_G.step()
        lr_scheduler_D_A.step()
        lr_scheduler_D_B.step()
        lr_scheduler_Det.step()

        # Save models checkpoints
        if (epoch+1)%5 == 0:
            torch.save(netG_A2B.state_dict(), 'output/netG_A2B_withdet_%d_0k_block.pth'%(epoch+1))
            torch.save(netG_B2A.state_dict(), 'output/netG_B2A_withdet_%d_0k_block.pth'%(epoch+1))
            torch.save(netD_A.state_dict(), 'output/netD_A_withdet_%d_0k_block.pth'%(epoch+1))
            torch.save(netD_B.state_dict(), 'output/netD_B_withdet_%d_0k_block.pth'%(epoch+1))
            torch.save(netDet.state_dict(), 'output/netDet_%d_0k_block.pth'%(epoch+1))
    ###################################
