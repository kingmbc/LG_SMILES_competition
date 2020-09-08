from pathlib import Path

# Data provided by LG
data_dir = Path('/home/jaeho_ubuntu/SMILES/data/')
train_dir = data_dir / 'train'
test_dir = data_dir / 'test'
train_csv_dir = data_dir /'train.csv'
train_pickle_dir = data_dir /'train_modified.pkl'
sample_submission_dir = data_dir /'sample_submission.csv'

# Data directory generated by us
input_data_dir = data_dir / 'input_data'
base_file_name = 'seed_123_max75smiles'

# Hyper parameter for generating data
random_seed = 123