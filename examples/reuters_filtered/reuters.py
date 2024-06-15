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
import pickle

from ptdec.dec import DEC
from ptdec.model import train, predict, target_distribution
from ptsdae.sdae import StackedDenoisingAutoEncoder
import ptsdae.model as ae
from ptdec.utils import cluster_accuracy

class ReutersDataset(Dataset):
    def __init__(self, features, labels, cuda):
        self.features = features
        self.labels = labels.squeeze()
        self.cuda = cuda

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        feature = self.features[idx]
        label = self.labels[idx]
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
@click.option(
    "--pkl-file",
    help="path to the filtered_reuters_dataset.pkl file.",
    type=str,
    default="filtered_reuters_dataset.pkl"
)
@click.option(
    "--target-cluster",
    help="the target cluster to get top scoring elements from (default 0).",
    type=int,
    default=0
)
def main(cuda, batch_size, pretrain_epochs, finetune_epochs, testing_mode, pkl_file, target_cluster):
    writer = SummaryWriter()  # create the TensorBoard object

    def training_callback(epoch, lr, loss, validation_loss):
        writer.add_scalars(
            "data/autoencoder",
            {"lr": lr, "loss": loss, "validation_loss": validation_loss,},
            epoch,
        )

    # Load the filtered dataset from the pickle file
    with open(pkl_file, 'rb') as f:
        features, labels = pickle.load(f)

    ds_train = ReutersDataset(features=features, labels=labels, cuda=cuda)  # training dataset
    ds_val = ReutersDataset(features=features, labels=labels, cuda=cuda)  # evaluation dataset
    autoencoder = StackedDenoisingAutoEncoder(
        [features.shape[1], 500, 500, 2000, 10], final_activation=None
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
        ds_train, model, 1024, silent=True, return_actual=True, cuda=cuda
    )
    actual = actual.cpu().numpy()
    predicted = predicted.cpu().numpy()
    reassignment, accuracy = cluster_accuracy(actual, predicted)
    print("Final DEC accuracy: %s" % accuracy)

    # Get the soft assignments
    model.eval()
    with torch.no_grad():
        q = model(ds_train.features)
        if cuda:
            q = q.cpu()
        q = q.numpy()

    # Get top 10 scoring elements from the target cluster
    top_indices = np.argsort(q[:, target_cluster])[-10:]
    top_scores = q[top_indices, target_cluster]

    print(f"Top 10 scoring elements in cluster {target_cluster}:")
    for idx, score in zip(top_indices, top_scores):
        print(f"Index: {idx}, Score: {score}")

    if not testing_mode:
        predicted_reassigned = [
            reassignment[item] for item in predicted
        ]
        confusion = confusion_matrix(actual, predicted_reassigned)
        normalised_confusion = (
            confusion.astype("float") / confusion.sum(axis=1)[:, np.newaxis]
        )
        confusion_id = uuid.uuid4().hex
        sns.heatmap(normalised_confusion).get_figure().savefig(
            "confusion_%s.png" % confusion_id
        )
        print("Writing out confusion diagram with UUID: %s" % confusion_id)
        writer.close()

if __name__ == "__main__":
    main()