# Lifelong Uncertainty-aware learning
import os,sys,time
import numpy as np
import copy
import math
import torch
import torch.nn.functional as F
from .bayesian_sgd import BayesianSGD



class Lul(object):

    def __init__(self, model, args, lr_min=1e-6, lr_factor=3, lr_patience=5, clipgrad=1000):
        self.model = model
        self.device = args.device
        self.lr_min = lr_min
        self.lr_factor = lr_factor
        self.lr_patience = lr_patience
        self.clipgrad = clipgrad

        self.init_lr = args.lr
        self.batch_size = args.batch_size
        self.n_epochs = args.n_epochs
        self.MC_samples = args.MC_samples

        self.output = args.output
        self.checkpoint = args.checkpoint
        self.num_tasks = args.num_tasks

        self.modules_names_with_cls = self.find_modules_names(with_classifier=True)
        self.modules_names_without_cls = self.find_modules_names(with_classifier=False)

    def train(self, task, xtrain, ytrain, xvalid, yvalid):

        # Update learning rate based on parameter uncertainty:
        params_dict = self.update_lr(task)
        self.optimizer = BayesianSGD(params=params_dict)

        # Best loss:
        best_loss = np.inf

        # Store best model
        best_model = copy.deepcopy(self.model.state_dict())
        lr = self.init_lr
        patience = self.lr_patience

        # Iterate over number of epochs
        try:
            for epoch in range(self.n_epochs):
                # Train
                clock0=time.time()
                self.train_epoch(task, xtrain, ytrain)
                clock1=time.time()
                train_loss = self.eval(task, xtrain, ytrain)
                clock2=time.time()

                print('| Epoch {:3d}, time={:5.1f}ms/{:5.1f}ms | Train: loss={:.3f} |'.format(epoch+1,
                    1000*self.batch_size*(clock1-clock0)/xtrain.size(0),1000*self.batch_size*(clock2-clock1)/xtrain.size(0),
                    train_loss),end='')

                # Validation:
                valid_loss = self.eval(task, xvalid, yvalid)
                print(' Valid: loss={:.3f} |'.format(valid_loss), end='')

                # Check if loss is nan
                if math.isnan(valid_loss) or math.isnan(train_loss):
                    print("Loss became nan, stopping training with saved model")
                    break

                # Adaptive learning rate:
                if valid_loss < best_loss:
                    best_loss = valid_loss
                    best_model = copy.deepcopy(self.model.state_dict())
                    patience = self.lr_patience
                    print(' *', end='')
                else:
                    patience -= 1
                    if patience <= 0:
                        lr /= self.lr_factor
                        print()
                        break
                    patience = self.lr_patience
                    params_dict = self.update_lr(task, adaptive_lr=True, lr=lr)
                    self.optimizer = BayesianSGD(params = params_dict)

                print()

        except KeyboardInterrupt:
            print()

        # Restore best model:
        self.model.load_state_dict(copy.deepcopy(best_model))
        self.save_model(task)

    def update_lr(self, task, adaptive_lr=False, lr=None):
        params_dict = []
        if task == 0:
            params_dict.append({'params': self.model.parameters(), 'lr': self.init_lr})
        else:
            # Iterate over layers in model
            for name in self.modules_names_without_cls:
                n = name.split('.')
                if len(n) == 1:
                    m = self.model._modules[n[0]]
                elif len(n) == 3:
                    m = self.model._modules[n[0]]._modules[n[1]]._modules[n[2]]
                elif len(n) == 4:
                    m = self.model._modules[n[0]]._modules[n[1]]._modules[n[2]]._modules[n[3]]
                else:
                    print (name)

                if adaptive_lr is True:
                    params_dict.append({'params': m.weight_rho, 'lr': lr})
                    params_dict.append({'params': m.bias_rho, 'lr': lr})
                else:
                    # Uncertainty in weights and bias:
                    w_uncertainty = torch.log1p(torch.exp(m.weight_rho.data))
                    b_uncertainty = torch.log1p(torch.exp(m.bias_rho.data))
                    # Update learning rate based on weight uncertainty
                    params_dict.append({'params': m.weight_mu, 'lr': torch.mul(w_uncertainty, self.init_lr)})
                    params_dict.append({'params': m.bias_mu, 'lr': torch.mul(b_uncertainty,self.init_lr)})
                    params_dict.append({'params': m.weight_rho, 'lr':self.init_lr})
                    params_dict.append({'params': m.bias_rho, 'lr':self.init_lr})

        return params_dict

    def find_modules_names(self, with_classifier=False):
        modules_names = []
        for name, p in self.model.named_parameters():
            if with_classifier is False:
                if not name.startswith('classifier'):
                    n = name.split('.')[:-1]
                    modules_names.append('.'.join(n))
            else:
                n = name.split('.')[:-1]
                modules_names.append('.'.join(n))

        modules_names = set(modules_names)
        return modules_names

    def logs(self, task):
        log_prior = 0.0
        log_variational_posterior = 0.0
        for name in self.modules_names_without_cls:
            n = name.split('.')
            if len(n) == 1:
                m = self.model._modules[n[0]]
            elif len(n) == 3:
                m = self.model._modules[n[0]]._modules[n[1]]._modules[n[2]]
            elif len(n) == 4:
                m = self.model._modules[n[0]]._modules[n[1]]._modules[n[2]]._modules[n[3]]

            log_prior += m.log_prior
            log_variational_posterior += m.log_variational_posterior

        log_prior += self.model.classifier[task].log_prior
        log_variational_posterior += self.model.classifier[task].log_variational_posterior

        return log_prior, log_variational_posterior

    def train_epoch(self, task, x, y):
        self.model.train()

        # Variable for shuffling
        index = np.arange(x.size(0))
        np.random.shuffle(index)
        index = torch.LongTensor(index).to(self.device)

        num_batches = len(x)//self.batch_size
        j = 0
        # Iterate over batches
        for i in range(0, len(index), self.batch_size):
            print("\rBatch: " + str(i)+"/"+str(len(index)), end=" ")
            if i + self.batch_size <= len(index):
                batch = index[i:i + self.batch_size]
            else:
                batch = index[i:]
            inputs, targets, = x[batch].to(self.device), y[batch].to(self.device)

            # Forward pass:
            loss = self.elbo_loss(inputs, targets, task, num_batches, sample=True).to(self.device)

            # Backward pass:
            #self.model.cuda()
            self.optimizer.zero_grad()
            loss.backward(retain_graph=True)
            #self.model.cuda()

            # Gradient step:
            self.optimizer.step()

    def eval(self, task, x, y, debug=False):
        total_loss = 0
        total_acc = 0
        total_num = 0
        self.model.eval()

        index = np.arange(x.size(0))
        index = torch.as_tensor(index, device=self.device, dtype=torch.int64)

        with torch.no_grad():
            num_batches = len(x)//self.batch_size
            # Iterate over batches
            for i in range(0, len(index), self.batch_size):
                if i + self.batch_size <= len(index):
                    batch = index[i:i + self.batch_size]
                else:
                    batch = index[i:]
                inputs, targets, = x[batch].to(self.device), y[batch].to(self.device)

                # Forward pass:
                outputs = self.model(inputs, sample = False)
                output = outputs[task]
                loss = self.elbo_loss(inputs, targets, task, num_batches, sample=False, debug=debug)

                total_loss += loss.detach()*len(batch)
                total_num += len(batch)

        return total_loss/total_num

    def set_model_(model, state_dict):
        model.model.load_state_dict(copy.deepcopy(state_dict))

    def my_loss(self, outputs, target):
        len_target = outputs.shape[1]
        # Get log varience
        log_variance = outputs[:,len_target//2:]
        # Mean term
        mean = outputs[:,0:len_target//2]
        # Residual regression term
        res_loss = torch.mean(0.5*torch.exp(-log_variance)*torch.square(target-mean), axis = 1)
        # Uncertainty loss
        unc_loss = torch.mean(0.5*torch.exp(log_variance))
        # Combined loss:
        loss = res_loss + unc_loss
        # Average over batch:
        loss = torch.mean(loss)
        return loss

    def elbo_loss(self, input, target, task, num_batches, sample, debug=False):
        if sample:
            log_priors, log_variational_posteriors, predictions = [], [], []
            for i in range(self.MC_samples):
                predictions.append(self.model(input, sample=sample)[task])
                log_prior, log_variational_posterior = self.logs(task)
                log_priors.append(log_prior)
                log_variational_posteriors.append(log_variational_posterior)

            # Coefficients, not sure why:
            w1 = 1.e-3
            w2 = 1.e-3
            w3 = 5.e-2

            outputs = torch.stack(predictions, dim=0).to(self.device)
            log_var = w1*torch.as_tensor(log_variational_posteriors, device=self.device).mean()
            log_p = w2*torch.as_tensor(log_priors, device=self.device).mean()

            # This is where a custom loss function must be implemented:
            #negative_log_likelihood = w3*torch.nn.functional.nll_loss(outputs.mean(0), target, reduction='sum').to(device=self.device)
            loss = self.my_loss(outputs.mean(0), target).to(device=self.device)

            return (log_var - log_p)/num_batches + loss

        else:
            predictions = []
            for i in range(self.MC_samples):
                predictions.append(self.model(input, sample=False)[task])

            w3 = 5.e-6

            outputs = torch.stack(predictions, dim=0).to(self.device)

            #negative_log_likelihood = w3*torch.nn.functional.nll_loss(outputs.mean(0), target, reduction='sum').to(device = self.device)
            loss = self.my_loss(outputs.mean(0), target).to(device=self.device)

            return loss

    def save_model(self, task):
        torch.save({'model_state_dict': self.model.state_dict(),
        }, os.path.join(self.checkpoint, 'model_{}.pth.tar'.format(task)))

    def log_softmax_likelihood(yhat_linear, y):
        return nd.nansum(y * nd.log_softmax(yhat_linear), axis=0, exclude=True)