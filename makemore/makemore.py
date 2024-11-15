"""
you give this script some words (one per line) and it will generate more things like it.
uses super state of the art Transformer AI tech
this code is intended to be super hackable. tune it to your needs.

Changes from minGPT:
- I removed the from_pretrained function where we init with GPT2 weights
- I removed dropout layers because the models we train here are small,
	it's not necessary to understand at this stage and at this scale.
- I removed weight decay and all of the complexity around what parameters are
	and are not weight decayed. I don't believe this should make a massive
	difference at the scale that we operate on here.
"""

import os
import sys
import time
import math
import argparse
from dataclasses import dataclass
from typing import List

import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.utils.data import Dataset
from torch.utils.data.dataloader import DataLoader
from torch.utils.tensorboard import SummaryWriter

# -----------------------------------------------------------------------------

@dataclass
class ModelConfig:
	block_size: int = None # length of the input sequences of integers
	vocab_size: int = None # the input integers are in range [0 .. vocab_size -1]
	# parameters below control the sizes of each model slightly differently
	n_layer: int = 4
	n_embd: int = 4
	n_embd1: int = 64
	n_embd2: int = 64
	n_head: int = 4

# -----------------------------------------------------------------------------
# Trigram language model

class Trigram(nn.Module):
	"""
	Trigram Language Model 'neural net', simply a lookup table of logits for the
	next character given a previous character.
	"""

	def __init__(self, config):
		super().__init__()
		self.n = config.vocab_size
		self.W = nn.Parameter(torch.randn(self.n*self.n, self.n))

	def get_block_size(self):
		return 2 # this model needs 2 previous character to predict the next

	def include_index(self, idx):
		return torch.cat([torch.zeros(idx.size(0), 1, dtype=int),
			self.n*idx[:,:-1]+idx[:,1:]], dim=1)


	def forward(self, idx, targets=None):
		idx_tg = self.include_index(idx)
		logits = self.W[idx_tg]

		# if we are given some desired targets also calculate the loss
		loss = None
		if targets is not None:
			loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)

		return logits, loss


# Bigram language model

class Bigram(nn.Module):
	"""
	Bigram Language Model 'neural net', simply a lookup table of logits for the
	next character given a previous character.
	"""

	def __init__(self, config):
		super().__init__()
		n = config.vocab_size
		self.W = nn.Parameter(torch.randn(n, n))

	def get_block_size(self):
		return 1 # this model only needs one previous character to predict the next

	def forward(self, idx, targets=None):

		 # 'forward pass', lol
		logits = self.W[idx]

		# if we are given some desired targets also calculate the loss
		loss = None
		if targets is not None:
			loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)

		return logits, loss

# MLP language model

class MLP(nn.Module):
	"""
	MLP Language Model. Two fully-connected layers with tanh non-linearity.
	"""

	def __init__(self, config):
		super().__init__()
		self.block_size = self.get_block_size()
		self.vocab_size = config.vocab_size
		self.wte = nn.Embedding(config.vocab_size+1, config.n_embd)
		self.mlp = nn.Sequential(
			nn.Linear(self.block_size * config.n_embd, config.n_embd2),
			nn.Tanh(),
			nn.Linear(config.n_embd2, self.vocab_size)
				)

	def get_block_size(self):
		return 3

	def forward(self, idx, targets=None):
		# Create embeddings by taking each position with its previous context up to `block_size`
		embs = [self.wte(idx[:, max(i - self.block_size + 1, 0):i + 1]) for i in range(idx.size(1))]

		# Pad each embedding block to `block_size` if necessary
		for i, emb in enumerate(embs):
				if emb.size(1) < self.block_size:
						padding = torch.zeros(emb.size(0), self.block_size - emb.size(1), emb.size(2)).to(emb.device)
						embs[i] = torch.cat((padding, emb), dim=1)

		# Concatenate embeddings
		x = torch.cat([emb.reshape(idx.size(0), 1, -1) for emb in embs], dim=1)  # shape: (b, t, block_size * n_embd)
	
	# # gather the word embeddings of the previous 3 words
	# 	embs = []
	# 	idxx = []
	# 	for k in range(self.block_size):
	# 		tok_emb = self.wte(idx) # token embeddings of shape (b, t, n_embd)
	# 		idx = torch.roll(idx, 1, 1)
	# 		idx[:, 0] = self.vocab_size # special <BLANK> token
	# 		embs.append(tok_emb)
	# 	# concat all of the embeddings together and pass through an MLP
	# 	x = torch.cat(embs, -1) # (b, t, n_embd * block_size)
		logits = self.mlp(x)

		# if we are given some desired targets also calculate the loss
		loss = None
		if targets is not None:
			loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)

		return logits, loss


# -----------------------------------------------------------------------------
# helper functions for evaluating and sampling from the model

@torch.no_grad()
def generate(model, idx, max_new_tokens, temperature=1.0, do_sample=False, top_k=None):
	"""
	Take a conditioning sequence of indices idx (LongTensor of shape (b,t)) and complete
	the sequence max_new_tokens times, feeding the predictions back into the model each time.
	Most likely you'll want to make sure to be in model.eval() mode of operation for this.
	"""
	block_size = model.get_block_size()
	for _ in range(max_new_tokens):
		# if the sequence context is growing too long we must crop it at block_size
		idx_cond = idx if idx.size(1) <= block_size else idx[:, -block_size:]
		# forward the model to get the logits for the index in the sequence
		logits, _ = model(idx_cond)
		# pluck the logits at the final step and scale by desired temperature
		logits = logits[:, -1, :] / temperature
		# optionally crop the logits to only the top k options
		if top_k is not None:
			v, _ = torch.topk(logits, top_k)
			logits[logits < v[:, [-1]]] = -float('Inf')
		# apply softmax to convert logits to (normalized) probabilities
		probs = F.softmax(logits, dim=-1)
		# either sample from the distribution or take the most likely element
		if do_sample:
			idx_next = torch.multinomial(probs, num_samples=1)
		else:
			_, idx_next = torch.topk(probs, k=1, dim=-1)
		# append sampled index to the running sequence and continue
		idx = torch.cat((idx, idx_next), dim=1)

	return idx

def print_samples(num=10):
	""" samples from the model and pretty prints the decoded samples """
	X_init = torch.zeros(num, 1, dtype=torch.long).to(args.device)
	top_k = args.top_k if args.top_k != -1 else None
	steps = train_dataset.get_output_length() - 1 # -1 because we already start with <START> token (index 0)
	X_samp = generate(model, X_init, steps, top_k=top_k, do_sample=True).to('cpu')
	train_samples, test_samples, new_samples = [], [], []
	for i in range(X_samp.size(0)):
		# get the i'th row of sampled integers, as python list
		row = X_samp[i, 1:].tolist() # note: we need to crop out the first <START> token
		# token 0 is the <STOP> token, so we crop the output sequence at that point
		crop_index = row.index(0) if 0 in row else len(row)
		row = row[:crop_index]
		word_samp = train_dataset.decode(row)
		# separately track samples that we have and have not seen before
		if train_dataset.contains(word_samp):
			train_samples.append(word_samp)
		elif test_dataset.contains(word_samp):
			test_samples.append(word_samp)
		else:
			new_samples.append(word_samp)
	print('-'*80)
	for lst, desc in [(train_samples, 'in train'), (test_samples, 'in test'), (new_samples, 'new')]:
		print(f"{len(lst)} samples that are {desc}:")
		for word in lst:
			print(word)
	print('-'*80)

@torch.inference_mode()
def evaluate(model, dataset, batch_size=50, max_batches=None):
	model.eval()
	loader = DataLoader(dataset, shuffle=True, batch_size=batch_size, num_workers=0)
	losses = []
	for i, batch in enumerate(loader):
		batch = [t.to(args.device) for t in batch]
		X, Y = batch
		logits, loss = model(X, Y)
		losses.append(loss.item())
		if max_batches is not None and i >= max_batches:
			break
	mean_loss = torch.tensor(losses).mean().item()
	model.train() # reset model back to training mode
	return mean_loss

# -----------------------------------------------------------------------------
# helper functions for creating the training and test Datasets that emit words

class CharDataset(Dataset):

	def __init__(self, words, chars, max_word_length):
		self.words = words
		self.chars = chars
		self.max_word_length = max_word_length
		self.stoi = {ch:i+1 for i,ch in enumerate(chars)}
		self.itos = {i:s for s,i in self.stoi.items()} # inverse mapping

	def __len__(self):
		return len(self.words)

	def contains(self, word):
		return word in self.words

	def get_vocab_size(self):
		return len(self.chars) + 1 # all the possible characters and special 0 token

	def get_output_length(self):
		return self.max_word_length + 1 # <START> token followed by words

	def encode(self, word):
		ix = torch.tensor([self.stoi[w] for w in word], dtype=torch.long)
		return ix

	def decode(self, ix):
		word = ''.join(self.itos[i] for i in ix)
		return word

	def __getitem__(self, idx):
		word = self.words[idx]
		ix = self.encode(word)
		x = torch.zeros(self.max_word_length + 1, dtype=torch.long)
		y = torch.zeros(self.max_word_length + 1, dtype=torch.long)
		x[1:1+len(ix)] = ix
		y[:len(ix)] = ix
		y[len(ix)+1:] = -1 # index -1 will mask the loss at the inactive locations
		return x, y

def create_datasets(input_file):

	# preprocessing of the input text file
	with open(input_file, 'r') as f:
		data = f.read()
	words = data.splitlines()
	words = [w.strip() for w in words] # get rid of any leading or trailing white space
	words = [w for w in words if w] # get rid of any empty strings
	chars = sorted(list(set(''.join(words)))) # all the possible characters
	max_word_length = max(len(w) for w in words)
	print(f"number of examples in the dataset: {len(words)}")
	print(f"max word length: {max_word_length}")
	print(f"number of unique characters in the vocabulary: {len(chars)}")
	print("vocabulary:")
	print(''.join(chars))

	# partition the input data into a training and the test set
	test_set_size = min(1000, int(len(words) * 0.1)) # 10% of the training set, or up to 1000 examples
	rp = torch.randperm(len(words)).tolist()
	train_words = [words[i] for i in rp[:-test_set_size]]
	test_words = [words[i] for i in rp[-test_set_size:]]
	print(f"split up the dataset into {len(train_words)} training examples and {len(test_words)} test examples")

	# wrap in dataset objects
	train_dataset = CharDataset(train_words, chars, max_word_length)
	test_dataset = CharDataset(test_words, chars, max_word_length)

	return train_dataset, test_dataset

class InfiniteDataLoader:
	"""
	this is really hacky and I'm not proud of it, but there doesn't seem to be
	a better way in PyTorch to just create an infinite dataloader?
	"""

	def __init__(self, dataset, **kwargs):
		train_sampler = torch.utils.data.RandomSampler(dataset, replacement=True, num_samples=int(1e10))
		self.train_loader = DataLoader(dataset, sampler=train_sampler, **kwargs)
		self.data_iter = iter(self.train_loader)

	def next(self):
		try:
			batch = next(self.data_iter)
		except StopIteration: # this will technically only happen after 1e10 samples... (i.e. basically never)
			self.data_iter = iter(self.train_loader)
			batch = next(self.data_iter)
		return batch

# -----------------------------------------------------------------------------
if __name__ == '__main__':

	# parse command line args
	parser = argparse.ArgumentParser(description="Make More")
	# system/input/output
	parser.add_argument('--input-file', '-i', type=str, default='names.txt', help="input file with things one per line")
	parser.add_argument('--work-dir', '-o', type=str, default='../../results/LLMs/makemore', help="output working directory")
	parser.add_argument('--resume', action='store_true', help="when this flag is used, we will resume optimization from existing model in the workdir")
	parser.add_argument('--sample-only', action='store_true', help="just sample from the model and quit, don't train")
	parser.add_argument('--num-workers', '-n', type=int, default=4, help="number of data workers for both train/test")
	parser.add_argument('--max-steps', type=int, default=-1, help="max number of optimization steps to run for, or -1 for infinite.")
	parser.add_argument('--device', type=str, default='cpu', help="device to use for compute, examples: cpu|cuda|cuda:2|mps")
	parser.add_argument('--seed', type=int, default=3407, help="seed")
	# sampling
	parser.add_argument('--top-k', type=int, default=-1, help="top-k for sampling, -1 means no top-k")
	# model
	parser.add_argument('--type', type=str, default='bigram', help="model class type to use, bigram|mlp|rnn|gru|bow|transformer")
	parser.add_argument('--n-layer', type=int, default=4, help="number of layers")
	parser.add_argument('--n-head', type=int, default=4, help="number of heads (in a transformer)")
	parser.add_argument('--n-embd', type=int, default=64, help="number of feature channels in the model")
	parser.add_argument('--n-embd1', type=int, default=64, help="number of feature channels elsewhere in the model")
	parser.add_argument('--n-embd2', type=int, default=64, help="number of feature channels elsewhere in the model")
	# optimization
	parser.add_argument('--batch-size', '-b', type=int, default=32, help="batch size during optimization")
	parser.add_argument('--learning-rate', '-l', type=float, default=5e-4, help="learning rate")
	parser.add_argument('--weight-decay', '-w', type=float, default=0.01, help="weight decay")
	args = parser.parse_args()
	print(vars(args))

	# system inits
	torch.manual_seed(args.seed)
	torch.cuda.manual_seed_all(args.seed)
	os.makedirs(args.work_dir, exist_ok=True)
	writer = SummaryWriter(log_dir=args.work_dir)

	# init datasets
	train_dataset, test_dataset = create_datasets(args.input_file)
	vocab_size = train_dataset.get_vocab_size()
	block_size = train_dataset.get_output_length()
	print(f"dataset determined that: {vocab_size=}, {block_size=}")

	# init model
	config = ModelConfig(vocab_size=vocab_size, block_size=block_size,
						 n_layer=args.n_layer, n_head=args.n_head,
						 n_embd=args.n_embd1, n_embd2=args.n_embd2)
	if args.type == 'bigram':
		model = Bigram(config)
	elif args.type == 'trigram':
		model = Trigram(config)
	elif args.type == 'MLP':
		model = MLP(config)
	else:
		raise ValueError(f'model type {args.type} is not recognized')
	model.to(args.device)
	print(f"model #params: {sum(p.numel() for p in model.parameters())}")
	if args.resume or args.sample_only: # note: if we sample-only then we also assume we are resuming
		print("resuming from existing model in the workdir")
		model.load_state_dict(torch.load(os.path.join(args.work_dir, 'model.pt')))
	if args.sample_only:
		print_samples(num=50)
		sys.exit()

	# init optimizer
	optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay, betas=(0.9, 0.99), eps=1e-8)

	# init dataloader
	batch_loader = InfiniteDataLoader(train_dataset, batch_size=args.batch_size, pin_memory=True, num_workers=args.num_workers)

	# training loop
	best_loss = None
	step = 0
	while True:

		t0 = time.time()

		# get the next batch, ship to device, and unpack it to input and target
		batch = batch_loader.next()
		batch = [t.to(args.device) for t in batch]
		X, Y = batch
		# feed into the model
		logits, loss = model(X, Y)

		# calculate the gradient, update the weights
		model.zero_grad(set_to_none=True)
		loss.backward()
		optimizer.step()

		# wait for all CUDA work on the GPU to finish then calculate iteration time taken
		if args.device.startswith('cuda'):
			torch.cuda.synchronize()
		t1 = time.time()

		# logging
		if step % 10 == 0:
			print(f"step {step} | loss {loss.item():.4f} | step time {(t1-t0)*1000:.2f}ms")

		# evaluate the model
		if step > 0 and step % 500 == 0:
			train_loss = evaluate(model, train_dataset, batch_size=100, max_batches=10)
			test_loss  = evaluate(model, test_dataset,  batch_size=100, max_batches=10)
			writer.add_scalar("Loss/train", train_loss, step)
			writer.add_scalar("Loss/test", test_loss, step)
			writer.flush()
			print(f"step {step} train loss: {train_loss} test loss: {test_loss}")
			# save the model to disk if it has improved
			if best_loss is None or test_loss < best_loss:
				out_path = os.path.join(args.work_dir, "model.pt")
				print(f"test loss {test_loss} is the best so far, saving model to {out_path}")
				torch.save(model.state_dict(), out_path)
				best_loss = test_loss

		# sample from the model
		if step > 0 and step % 200 == 0:
			print_samples(num=10)

		step += 1
		# termination conditions
		if args.max_steps >= 0 and step >= args.max_steps:
			break
