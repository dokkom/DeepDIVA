# Utils
import inspect
import json
import logging
import os
import random
import shutil
import sys
import tarfile
import tempfile
import time

import colorlog
import numpy as np
import pandas as pd
# Torch related stuff
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
from tensorboardX import SummaryWriter

# DeepDIVA
import models
from datasets import image_folder_dataset, bidimensional_dataset
from util.dataset_analytics import compute_mean_std
from util.misc import get_all_files_in_folders_subfolders


def set_up_model(output_channels, model_name, pretrained, optimizer_name, no_cuda, resume, load_model, start_epoch,
                 train_loader, disable_databalancing, dataset_folder, inmem, workers, num_classes=None, **kwargs):
    """
    Instantiate model, optimizer, criterion. Load a pretrained model or resume from a checkpoint.

    Parameters
    ----------
    :param output_channels: int
        Specify shape of final layer of network. Only used if num_classes is not specified.

    :param model_name: string
        Name of the model

    :param pretrained: bool
        Specify whether to load a pretrained model or not

    :param optimizer_name: string
        Name of the optimizer

    :param lr: float
        Value for learning rate

    :param no_cuda: bool
        Specify whether to use the GPU or not

    :param resume: string
        Path to a saved checkpoint

    :param load_model: string
        Path to a saved model

    :param start_epoch
        Epoch from which to resume training. If if not resuming a previous experiment the value is 0

    :param num_classes: int
        Number of classes for the model

    :param kwargs: dict
        Any additional arguments.

    :return: model, criterion, optimizer, best_value, start_epoch
    """

    # Initialize the model
    logging.info('Setting up model {}'.format(model_name))

    output_channels = output_channels if num_classes == None else num_classes
    model = models.__dict__[model_name](output_channels=output_channels, pretrained=pretrained)

    # Get the optimizer created with the specified parameters in kwargs (such as lr, momentum, ... )
    optimizer = _get_optimizer(optimizer_name, model, **kwargs)

    # Get the criterion
    if disable_databalancing:
        criterion = nn.CrossEntropyLoss()
    else:
        weights = _load_class_frequencies_weights_from_file(dataset_folder, inmem, workers)
        criterion = nn.CrossEntropyLoss(weight=torch.from_numpy(weights).type(torch.FloatTensor))

    # Transfer model to GPU (if desired)
    if not no_cuda:
        logging.info('Transfer model to GPU')
        model = torch.nn.DataParallel(model).cuda()
        criterion = criterion.cuda()
        cudnn.benchmark = True

    # Load saved model
    if load_model:
        if os.path.isfile(load_model):
            model_dict = torch.load(load_model)
            logging.info('Loading a saved model')
            try:
                model.load_state_dict(model_dict['state_dict'])
            except:
                logging.info('Loading model in compatibility mode')
                if not no_cuda:
                    model.module.load_pretrained_state_dict(model_dict['state_dict'])
                else:
                    model.load_pretrained_state_dict(model_dict['state_dict'])
        else:
            logging.error("No model dict found at '{}'".format(load_model))
            sys.exit(-1)

    # Resume from checkpoint
    if resume:
        if os.path.isfile(resume):
            logging.info("Loading checkpoint '{}'".format(resume))
            checkpoint = torch.load(resume)
            start_epoch = checkpoint['epoch']
            best_value = checkpoint['best_value']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            # val_losses = [checkpoint['val_loss']] #not used?
            logging.info("Loaded checkpoint '{}' (epoch {})".format(resume, checkpoint['epoch']))
        else:
            logging.error("No checkpoint found at '{}'".format(resume))
            sys.exit(-1)
    else:
        best_value = 0.0

    return model, criterion, optimizer, best_value, start_epoch


def _load_class_frequencies_weights_from_file(dataset_folder, inmem, workers):
    """
    This function simply recovers class_frequencies_weights from the analytics.csv file

    Parameters:
    -----------
    :param dataset_folder: string
        Path string that points to the three folder train/val/test. Example: ~/../../data/svhn

    :param inmem: boolean
        Flag: if False, the dataset is loaded in an online fashion i.e. only file names are stored and images are loaded
        on demand. This is slower than storing everything in memory.

    :param workers: int
        Number of workers to use for the mean/std computation

    :return: double[], double[]
        Mean and Std of the selected dataset, contained in the analytics.csv file. These are double arrays.
    """
    csv_file = _load_analytics_csv(dataset_folder, inmem, workers)
    return csv_file.ix[2, 1:].as_matrix().astype(float)


def _get_optimizer(optimizer_name, model, **kwargs):
    """
    This function serves as interface between the command line and the optimizer.
    In fact each optimizer has a different set of parameters and in this way one can just change the optimizer
    in his experiments just by changing the parameters passed to the entry point.
    :param optimizer_name:
        Name of the optimizers. See: torch.optim for a list of possible values
    :param model:
        The model with which the training will be done
    :param kwargs:
        List of all arguments to be used to init the optimizer
    :return:
        The optimizer initialized with the provided parameters
    """
    # Verify the optimizer exists
    assert optimizer_name in torch.optim.__dict__

    params = {}
    # For all arguments declared in the constructor signature of the selected optimizer
    for p in inspect.getfullargspec(torch.optim.__dict__[optimizer_name].__init__).args:
        # Add it to a dictionary in case it exists a corresponding value in kwargs
        if p in kwargs:
            params.update({p: kwargs[p]})
    # Create an return the optimizer with the correct list of parameters
    return torch.optim.__dict__[optimizer_name](model.parameters(), **params)


def set_up_dataloaders(model_expected_input_size, dataset_folder, batch_size, workers, inmem=False, **kwargs):
    """
    Set up the dataloaders for the specified datasets.

    Parameters
    ----------
    :param model_expected_input_size: tuple
        Specify the height and width that the model expects.

    :param dataset_folder: string
        Path string that points to the three folder train/val/test. Example: ~/../../data/svhn

    :param batch_size: int
        Number of datapoints to process at once

    :param workers: int
        Number of workers to use for the dataloaders

    :param inmem: boolean
        Flag: if False, the dataset is loaded in an online fashion i.e. only file names are stored and images are loaded
        on demand. This is slower than storing everything in memory.

    :param kwargs: dict
        Any additional arguments.

    :return: dataloader, dataloader, dataloader, int
        Three dataloaders for train, val and test. Number of classes for the model.
    """

    # Recover dataset name
    dataset = os.path.basename(os.path.normpath(dataset_folder))
    logging.info('Loading {} from:{}'.format(dataset, dataset_folder))

    ###############################################################################################
    # Load the dataset splits as images
    try:
        logging.debug("Try to load dataset as images")
        train_ds, val_ds, test_ds = image_folder_dataset.load_dataset(dataset_folder, inmem, workers)

        # Loads the analytics csv and extract mean and std
        mean, std = _load_mean_std_from_file(dataset_folder, inmem, workers)

        # Set up dataset transforms
        logging.debug('Setting up dataset transforms')
        train_ds.transform = transforms.Compose([
            transforms.Resize(model_expected_input_size),
            transforms.ToTensor(),
            transforms.Normalize(mean=mean, std=std)
        ])

        val_ds.transform = transforms.Compose([
            transforms.Resize(model_expected_input_size),
            transforms.ToTensor(),
            transforms.Normalize(mean=mean, std=std)
        ])

        test_ds.transform = transforms.Compose([
            transforms.Resize(model_expected_input_size),
            transforms.ToTensor(),
            transforms.Normalize(mean=mean, std=std)
        ])

        test_loader, train_loader, val_loader = _dataloaders_from_datasets(batch_size, train_ds, val_ds, test_ds,
                                                                           workers)
        logging.info("Dataset loaded as images")
        return train_loader, val_loader, test_loader, len(train_ds.classes)

    except RuntimeError:
        logging.debug("No images found in dataset folder provided")

    ###############################################################################################
    # Load the dataset splits as bidimensional
    try:
        logging.debug("Try to load dataset as bidimensional")
        train_ds, val_ds, test_ds = bidimensional_dataset.load_dataset(dataset_folder)

        # Loads the analytics csv and extract mean and std
        # TODO: update bidimensional to work with new load_mean_std functions
        mean, std = _load_mean_std_from_file(dataset_folder, inmem, workers)

        # Bring mean and std into range [0:1] from original domain
        mean = np.divide((mean - train_ds.min_coords), np.subtract(train_ds.max_coords, train_ds.min_coords))
        std = np.divide((std - train_ds.min_coords), np.subtract(train_ds.max_coords, train_ds.min_coords))

        # Set up dataset transforms
        logging.debug('Setting up dataset transforms')
        train_ds.transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=mean, std=std)
        ])

        val_ds.transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=mean, std=std)
        ])

        test_ds.transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=mean, std=std)
        ])

        test_loader, train_loader, val_loader = _dataloaders_from_datasets(batch_size, train_ds, val_ds, test_ds,
                                                                           workers)
        logging.info("Dataset loaded as bidimensional data")
        return train_loader, val_loader, test_loader, len(train_ds.classes)

    except RuntimeError:
        logging.debug("No bidimensional found in dataset folder provided")

    ###############################################################################################
    # Verify that eventually a dataset has been correctly loaded
    logging.error("No datasets have been loaded. Verify dataset folder location or dataset folder structure")
    sys.exit(-1)


def _load_mean_std_from_file(dataset_folder, inmem, workers):
    """
    This function simply recovers mean and std from the analytics.csv file

    Parameters:
    -----------
    :param dataset_folder: string
        Path string that points to the three folder train/val/test. Example: ~/../../data/svhn

    :param inmem: boolean
        Flag: if False, the dataset is loaded in an online fashion i.e. only file names are stored and images are loaded
        on demand. This is slower than storing everything in memory.

    :param workers: int
        Number of workers to use for the mean/std computation

    :return: double[], double[]
        Mean and Std of the selected dataset, contained in the analytics.csv file. These are double arrays.
    """
    # Loads the analytics csv and extract mean and std
    csv_file = _load_analytics_csv(dataset_folder, inmem, workers)
    mean = np.asarray(csv_file.ix[0, 1:3])
    std = np.asarray(csv_file.ix[1, 1:3])
    return mean, std


def _load_analytics_csv(dataset_folder, inmem, workers):
    """
        This function simply loads the analytics.csv file and attempts creating it if it misses

        Parameters:
        -----------
        :param dataset_folder: string
            Path string that points to the three folder train/val/test. Example: ~/../../data/svhn

        :param inmem: boolean
            Flag: if False, the dataset is loaded in an online fashion i.e. only file names are stored and images are loaded
            on demand. This is slower than storing everything in memory.

        :param workers: int
            Number of workers to use for the mean/std computation

        :return:
            The csv file
        """
    # If analytics.csv file not present, run the analytics on the dataset
    if not os.path.exists(os.path.join(dataset_folder, "analytics.csv")):
        logging.warning('Missing analytics.csv file for dataset located at {}'.format(dataset_folder))
        try:
            logging.warning('Attempt creating analytics.csv file for dataset located at {}'.format(dataset_folder))
            compute_mean_std(dataset_folder=dataset_folder, inmem=inmem, workers=workers)
            logging.warning('Created analytics.csv file for dataset located at {} '.format(dataset_folder))
        except:
            logging.error('Creation of analytics.csv failed.')
            sys.exit(-1)
    # Loads the analytics csv
    return pd.read_csv(os.path.join(dataset_folder, "analytics.csv"), header=None)


def _dataloaders_from_datasets(batch_size, train_ds, val_ds, test_ds, workers):
    """

    Parameters:
    -----------
    :param batch_size: int
        The size of the mini batch

    :param train_ds, val_ds, test_ds: torch.utils.data.Dataset
        The datasets split loaded, ready to be fed to a dataloader

    :param workers:
        Number of workers to use to load the data. If full reproducibility is desired select 1 (slower)

    :return: torch.utils.data.DataLoader[]
        The dataloaders for each split passed
    """
    # Setup dataloaders
    logging.debug('Setting up dataloaders')
    train_loader = torch.utils.data.DataLoader(train_ds,
                                               shuffle=True,
                                               batch_size=batch_size,
                                               num_workers=workers,
                                               pin_memory=True)
    val_loader = torch.utils.data.DataLoader(val_ds,
                                             batch_size=batch_size,
                                             num_workers=workers,
                                             pin_memory=True)
    test_loader = torch.utils.data.DataLoader(test_ds,
                                              batch_size=batch_size,
                                              num_workers=workers,
                                              pin_memory=True)
    return test_loader, train_loader, val_loader


#######################################################################################################################
def set_up_logging(parser, experiment_name, output_folder, quiet, args_dict, debug, **kwargs):
    """
    Set up a logger for the experiment

    Parameters
    ----------
    :param parser : parser
        The argument parser

    :param experiment_name: string
        Name of the experiment. If not specify, accepted from command line.

    :param output_folder: string
        Path to where all experiment logs are stored.

    :param quiet: bool
        Specify whether to print log to console or only to text file

    :param debug: bool
        Specify the logging level

    :param args_dict: dict
        Contains the entire argument dictionary specified via command line.

    :return: string
        log_folder, the final logging folder tree
    """
    LOG_FILE = 'logs.txt'

    # Experiment name override
    if experiment_name is None:
        experiment_name = input("Experiment name:")

    # Recover dataset name
    dataset = os.path.basename(os.path.normpath(kwargs['dataset_folder']))

    """
    We extract the TRAIN parameters names (such as model_name, lr, ... ) from the parser directly. 
    This is a somewhat risky operation because we access _private_variables of parsers classes.
    However, within our context this can be regarded as safe. 
    Shall we be wrong, a quick fix is writing a list of possible parameters such as:
    
        train_param_list = ['model_name','lr', ...] 
    
    and manually maintain it (boring!).
    
    Resources:
    https://stackoverflow.com/questions/31519997/is-it-possible-to-only-parse-one-argument-groups-parameters-with-argparse
    
    """

    # Fetch all non-default parameters
    non_default_parameters = []

    for group in parser._action_groups[2:]:
        if group.title not in ['GENERAL', 'DATA']:
            for action in group._group_actions:
                if (kwargs[action.dest] is not None) and (kwargs[action.dest] != action.default) and action.dest != 'load_model':
                    non_default_parameters.append(str(action.dest) + "=" + str(kwargs[action.dest]))

    # Build up final logging folder tree with the non-default training parameters
    log_folder = os.path.join(*[output_folder, experiment_name, dataset, *non_default_parameters,
                                '{}'.format(time.strftime('%d-%m-%y-%Hh-%Mm-%Ss'))])
    if not os.path.exists(log_folder):
        os.makedirs(log_folder)

    # Setup logging
    root = logging.getLogger()
    log_level = logging.DEBUG if debug else logging.INFO
    root.setLevel(log_level)
    format = "[%(asctime)s] [%(levelname)8s] --- %(message)s (%(filename)s:%(lineno)s)"
    date_format = '%Y-%m-%d %H:%M:%S'

    if os.isatty(2):
        cformat = '%(log_color)s' + format
        formatter = colorlog.ColoredFormatter(cformat, date_format,
                                              log_colors={
                                                  'DEBUG': 'cyan',
                                                  'INFO': 'white',
                                                  'WARNING': 'yellow',
                                                  'ERROR': 'red',
                                                  'CRITICAL': 'red,bg_white',
                                              })
    else:
        formatter = logging.Formatter(format, date_format)

    if not quiet:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        root.addHandler(ch)

    fh = logging.FileHandler(os.path.join(log_folder, LOG_FILE))
    fh.setFormatter(logging.Formatter(format, date_format))
    root.addHandler(fh)

    logging.info('Setup logging. Log file: {}'.format(os.path.join(log_folder, LOG_FILE)))

    # Save args to logs_folder
    logging.info('Arguments saved to: {}'.format(os.path.join(log_folder, 'args.txt')))
    with open(os.path.join(log_folder, 'args.txt'), 'w') as f:
        f.write(json.dumps(args_dict))

    # Define Tensorboard SummaryWriter
    logging.info('Initialize Tensorboard SummaryWriter')

    # Add all parameters to Tensorboard
    writer = SummaryWriter(log_dir=log_folder)
    writer.add_text('Args', json.dumps(args_dict))

    return log_folder, writer


def copy_code(output_folder):
    """
    Makes a tar file with DeepDIVA that exists during runtime.

    :param output_folder: str
        Path to output directory
    :return: None
    """
    # All file extensions to be saved by copy-code.
    FILE_TYPES = ['.sh', '.py']

    # Get DeepDIVA root
    cwd = os.getcwd()
    dd_root = os.path.join(cwd.split('DeepDIVA')[0], 'DeepDIVA')

    files = get_all_files_in_folders_subfolders(dd_root)

    # Get all files types in DeepDIVA as specified in FILE_TYPES
    code_files = [item for item in files if item.endswith(tuple(FILE_TYPES))]

    tmp_dir = tempfile.mkdtemp()

    for item in code_files:
        dest = os.path.join(tmp_dir, 'DeepDIVA', item.split('DeepDIVA')[1][1:])
        if not os.path.exists(os.path.dirname(dest)):
            os.makedirs(os.path.dirname(dest))
        shutil.copy(item, dest)

    # TODO: make it save a zipfile instead of a tarfile.
    with tarfile.open(os.path.join(output_folder, 'DeepDIVA.tar.gz'), 'w:gz') as tar:
        tar.add(tmp_dir, arcname='DeepDIVA')

    # Clean up all temporary files
    shutil.rmtree(tmp_dir)

    return






def set_up_env(gpu_id, seed, multi_run, no_cuda, **kwargs):
    """
    Set up the execution environment.

    Parameters
    ----------
    :param gpu_id: string
        Specify the GPUs to be used

    :param seed:    int
        Seed all possible seeds for deterministic run

    :param multi_run: int
        Number of runs over the same code to produce mean-variance graph.

    :param no_cuda: bool
        Specify whether to use the GPU or not

    :return: None
    """
    # Set visible GPUs
    if gpu_id is not None:
        os.environ['CUDA_VISIBLE_DEVICES'] = gpu_id

    # Seed the random
    if seed is None:
        # If seed is not specified by user, select a random value for the seed and then log it.
        seed = np.random.randint(2 ** 32 - 1, )
        logging.info('Randomly chosen seed is: {}'.format(seed))
    else:
        try:
            assert multi_run == None
        except:
            logging.warning('Arguments for seed AND multi-run should not be active at the same time!')
            raise SystemExit

        # Disable CuDNN only if seed is specified by user. Otherwise we can assume that the user does not want to
        # sacrifice speed for deterministic behaviour.
        # TODO: Check if setting torch.backends.cudnn.deterministic=True will ensure deterministic behavior.
        # Initial tests show torch.backends.cudnn.deterministic=True does not work correctly.
        if not no_cuda:
            torch.backends.cudnn.enabled = False

    # Python
    random.seed(seed)

    # Numpy random
    np.random.seed(seed)

    # Torch random
    torch.manual_seed(seed)
    if not no_cuda:
        torch.cuda.manual_seed_all(seed)

    return
