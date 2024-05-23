import click
import numpy as np
import seaborn as sns
from sklearn.metrics import confusion_matrix
from torch.optim import SGD
from torch.optim.lr_scheduler import StepLR
import torch
from torch.utils.data import Dataset
from tensorboardX import SummaryWriter
import uuid
import os
from datasets import load_dataset
from sklearn.feature_extraction.text import TfidfVectorizer

from ptdec.dec import DEC
from ptdec.model import train, predict
from ptsdae.sdae import StackedDenoisingAutoEncoder
import ptsdae.model as ae
from ptdec.utils import cluster_accuracy

class ReutersDataset(Dataset):
    def __init__(self, dataset, tfidf_features, cuda):
        self.data = dataset
        self.tfidf_features = tfidf_features
        self.cuda = cuda

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        feature = self.tfidf_features[idx]
        label = self.data[idx]["topics"]
        feature = torch.tensor(feature, dtype=torch.float)
        label = torch.tensor(label, dtype=torch.long)
        if self.cuda:
            feature = feature.cuda()
            label = label.cuda()
        return feature, label


@click.command()
@click.option(
    "--cuda", help="whether to use CUDA (default False).", type=bool, default=False
)
@click.option(
    "--batch-size", help="training batch size (default 256).", type=int, default=256
)
@click.option(
    "--pretrain-epochs",
    help="number of pretraining epochs (default 300).",
    type=int,
    default=300,
)
@click.option(
    "--finetune-epochs",
    help="number of finetune epochs (default 500).",
    type=int,
    default=500,
)
@click.option(
    "--testing-mode",
    help="whether to run in testing mode (default False).",
    type=bool,
    default=False,
)
def main(cuda, batch_size, pretrain_epochs, finetune_epochs, testing_mode):
    writer = SummaryWriter()  # create the TensorBoard object

    def training_callback(epoch, lr, loss, validation_loss):
        writer.add_scalars(
            "data/autoencoder",
            {"lr": lr, "loss": loss, "validation_loss": validation_loss,},
            epoch,
        )

    # Load the Reuters-21578 dataset
    dataset = load_dataset('reuters21578', 'ModApte')

    # Preprocessing
    texts = [doc["text"] for doc in dataset["train"]]
    vectorizer = TfidfVectorizer(max_features=2000)
    tfidf_features = vectorizer.fit_transform(texts).toarray()

    ds_train = ReutersDataset(dataset["train"], tfidf_features, cuda=cuda)  # training dataset
    ds_val = ReutersDataset(dataset["test"], tfidf_features, cuda=cuda)  # evaluation dataset
    autoencoder = StackedDenoisingAutoEncoder(
        [2000, 500, 500, 2000, 10], final_activation=None
    )
    if cuda:
        autoencoder.cuda()
    print("Pretraining stage.")
    ae.pretrain(
        ds_train,
        autoencoder,
        cuda=cuda,
        validation=ds_val,
        epochs=pretrain_epochs,
        batch_size=batch_size,
        optimizer=lambda model: SGD(model.parameters(), lr=0.1, momentum=0.9),
        scheduler=lambda x: StepLR(x, 100, gamma=0.1),
        corruption=0.2,
    )
    # Save the pretrained model
    torch.save(autoencoder.state_dict(), "pretrained_model.pth")

    print("Training stage.")
    ae_optimizer = SGD(params=autoencoder.parameters(), lr=0.1, momentum=0.9)
    ae.train(
        ds_train,
        autoencoder,
        cuda=cuda,
        validation=ds_val,
        epochs=finetune_epochs,
        batch_size=batch_size,
        optimizer=ae_optimizer,
        scheduler=StepLR(ae_optimizer, 100, gamma=0.1),
        corruption=0.2,
        update_callback=training_callback,
    )
    # Save the fine-tuned model
    torch.save(autoencoder.state_dict(), "finetuned_model.pth")

    print("DEC stage.")
    model = DEC(cluster_number=10, hidden_dimension=10, encoder=autoencoder.encoder)
    if cuda:
        model.cuda()
    dec_optimizer = SGD(model.parameters(), lr=0.01, momentum=0.9)
    train(
        dataset=ds_train,
        model=model,
        epochs=100,
        batch_size=256,
        optimizer=dec_optimizer,
        stopping_delta=0.000001,
        cuda=cuda,
    )
    # Save the DEC model
    torch.save(model.state_dict(), "dec_model.pth")

    predicted, actual = predict(
        ds_train, model, 1024, silent=True