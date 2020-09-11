import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence

from model.Network import Encoder, DecoderWithAttention
from utils import make_directory,decode_predicted_sequences

import numpy as np
import asyncio
import os

class MSTS:
    def __init__(self, config):
        # self._data_folder = config.data_folder
        # self._data_name = config.data_name

        self._vocab_size = 70
        self._emb_dim = config.emb_dim
        self._attention_dim = config.attention_dim
        self._decoder_dim = config.decoder_dim
        self._dropout = config.dropout
        self._device = config.device
        self._cudnn_benchmark = config.cudnn_benchmark

        self._start_epoch = config.start_epoch
        self._epochs = config.epochs
        self._epochs_since_improvement = config.epochs_since_improvement
        self._batch_size = config.batch_size
        self._workers = config.workers
        self._encoder_lr = config.encoder_lr
        self._decoder_lr = config.decoder_lr
        self._grad_clip = config.grad_clip
        self._alpha_c = config.alpha_c
        self._best_bleu4 = config.best_bleu4
        self._print_freq = config.print_freq
        self._fine_tune_encoder = config.fine_tune_encoder

        self._model_save_path = config.model_save_path
        self._model_load_path = config.model_load_path
        self._model_load_num = config.model_load_num

        self._model_name = self._model_name_maker()

        self._decoder = DecoderWithAttention(attention_dim=self._attention_dim,
                                             embed_dim=self._emb_dim,
                                             decoder_dim=self._decoder_dim,
                                             vocab_size=self._vocab_size,
                                             dropout=self._dropout)
        self._decoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad,
                                                           self._decoder.parameters()),
                                                           lr=self._decoder_lr)
        self._encoder = Encoder()
        self._encoder.fine_tune(self._fine_tune_encoder)
        self._encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad,
                                                           self._encoder.parameters()),
                                                           lr=self._encoder_lr) if self._fine_tune_encoder else None
        self._encoder.to(self._device)
        self._decoder.to(self._device)
        if torch.cuda.device_count() > 1:
            print("Let's use", torch.cuda.device_count(), "GPUs!")
            self._encoder = nn.DataParallel(self._encoder)
        #    self._decoder = nn.DataParallel(self._decoder)
        self._criterion = nn.CrossEntropyLoss().to(self._device)


    def _clip_gradient(self, optimizer, grad_clip):
        """
        Clips gradients computed during backpropagation to avoid explosion of gradients.

        :param optimizer: optimizer with the gradients to be clipped
        :param grad_clip: clip value
        """
        for group in optimizer.param_groups:
            for param in group['params']:
                if param.grad is not None:
                    param.grad.data.clamp_(-grad_clip, grad_clip)


    def train(self, train_loader):

        self._encoder.train()
        self._decoder.train()

        mean_loss = 0
        mean_accuracy = 0

        for i, (imgs, sequence, sequence_lens) in enumerate(train_loader):
            imgs = imgs.to(self._device)
            sequence = sequence.to(self._device)
            sequence_lens = sequence_lens.to(self._device)

            # Forward prop.
            imgs = self._encoder(imgs)
            predictions, caps_sorted, decode_lengths, alphas, sort_ind = self._decoder(imgs, sequence, sequence_lens)

            # if i%50 == 0:
            #     print('step:',i)
            #     print('predictions:', torch.argmax(predictions.detach().cpu(), -1).numpy()[0])
            #     print('target:', caps_sorted.detach().cpu().numpy()[0])

            # Since we decoded starting with <start>, the targets are all words after <start>, up to <end>
            targets = caps_sorted[:, 1:]

            # Calculate accuracy
            accr = self._accuracy_calcluator(predictions.detach().cpu().numpy(),
                                             targets.detach().cpu().numpy(),
                                             np.array(decode_lengths))

            mean_accuracy = mean_accuracy + (accr - mean_accuracy) / (i+1)


            # Remove timesteps that we didn't decode at, or are pads
            # pack_padded_sequence is an easy trick to do this
            predictions = pack_padded_sequence(predictions, decode_lengths, batch_first=True).data
            targets = pack_padded_sequence(targets, decode_lengths, batch_first=True).data

            # Calculate loss
            loss = self._criterion(predictions, targets)
            mean_loss = mean_loss + (loss - mean_loss)/(i+1)

            # Back prop.
            self._decoder_optimizer.zero_grad()
            self._encoder_optimizer.zero_grad()

            loss.backward()

            # Clip gradients
            if self._grad_clip is not None:
                self._clip_gradient(self._decoder_optimizer, self._grad_clip)
                self._clip_gradient(self._encoder_optimizer, self._grad_clip)

            # Update weights
            self._decoder_optimizer.step()
            self._encoder_optimizer.step()

        return mean_loss, mean_accuracy


    def validation(self, val_loader):
        self._encoder.eval()
        self._decoder.eval()

        mean_loss = 0
        mean_accuracy = 0

        for i, (imgs, sequence, sequence_lens) in enumerate(val_loader):
            imgs = imgs.to(self._device)
            sequence = sequence.to(self._device)
            sequence_lens = sequence_lens.to(self._device)

            imgs = self._encoder(imgs)
            predictions, caps_sorted, decode_lengths, _, _ = self._decoder(imgs, sequence, sequence_lens)

            targets = caps_sorted[:, 1:]

            accr = self._accuracy_calcluator(predictions.detach().cpu().numpy(),
                                             targets.detach().cpu().numpy(),
                                             np.array(decode_lengths))

            mean_accuracy = mean_accuracy + (accr - mean_accuracy) / (i + 1)

            predictions = pack_padded_sequence(predictions, decode_lengths, batch_first=True).data
            targets = pack_padded_sequence(targets, decode_lengths, batch_first=True).data

            loss = self._criterion(predictions, targets)
            mean_loss = mean_loss + (loss - mean_loss) / (i + 1)

        return mean_loss, mean_accuracy


    def model_test(self, submission, test_loader, reversed_token_map):

        self.model_load()
        self._encoder.eval()
        self._decoder.eval()


        for i, (imgs, sequence, sequence_lens) in enumerate(test_loader):
            imgs = imgs.to(self._device)
            sequence = sequence.to(self._device)
            sequence_lens = sequence_lens.to(self._device)

            imgs = self._encoder(imgs)
            predictions, _, _, _, _ = self._decoder(imgs, sequence, sequence_lens)
            SMILES_predicted_sequence = list(torch.argmax(predictions.detach().cpu(), -1).numpy())
            decoded_sequences = decode_predicted_sequences(SMILES_predicted_sequence,reversed_token_map)
            submission['SMILES'].loc[i] = decoded_sequences

        return submission


    def model_save(self, save_num):
        torch.save(
            self._decoder.state_dict(),
            '{}/'.format(self._model_save_path)+self._model_name+'/decoder{}.pkl'.format(str(save_num).zfill(3))
        )
        torch.save(
            self._encoder.state_dict(),
            '{}/'.format(self._model_save_path)+self._model_name+'/encoder{}.pkl'.format(str(save_num).zfill(3))
        )


    def model_load(self):
        self._decoder.load_state_dict(
            torch.load('{}/decoder{}.pkl'.format(self._model_load_path, str(self._model_load_num).zfill(3)))
        )
        self._encoder.load_state_dict(
            torch.load('{}/encoder{}.pkl'.format(self._model_load_path, str(self._model_load_num).zfill(3)))
        )


    def _model_name_maker(self):
        name = 'model-emb_dim_{}-attention_dim_{}-decoder_dim_{}-dropout_{}-batch_size_{}'.format(
            self._emb_dim, self._attention_dim, self._decoder_dim, self._dropout, self._batch_size)
        make_directory(self._model_save_path + '/' + name)

        return name


    def _accuracy_calcluator(self, prediction: np.array, target: np.array, decode_len: np.array):
        mean_accr = 0
        prediction = np.argmax(prediction,2)
        for itr, (p, t, l) in enumerate(zip(prediction, target, decode_len)):
            accr = 0
            for i in range(l):
                if np.argmax(p[i]) == t[i]:
                    accr = accr + (1 - accr) / (i+1)
            mean_accr = mean_accr + (accr - mean_accr) / (itr+1)

        return mean_accr