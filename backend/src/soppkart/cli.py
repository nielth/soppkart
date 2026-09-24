"""Command line entry point: `soppkart {fetch,features,train,all}`."""

import argparse
import logging

from soppkart import config, features, sources, train
from soppkart.grid import Grid


def main() -> None:
    parser = argparse.ArgumentParser(prog="soppkart", description="Kantarell map pipeline")
    parser.add_argument(
        "step",
        choices=["fetch", "features", "train", "all"],
        help="fetch: download source data, features: build feature rasters, "
        "train: train model and write score map, all: everything",
    )
    parser.add_argument(
        "--species",
        choices=list(config.SPECIES),
        help="only train this species (default: all)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.step in ("fetch", "all"):
        sources.fetch_all()
    if args.step in ("features", "all"):
        features.build_features(Grid.norway())
    if args.step in ("train", "all"):
        keys = [args.species] if args.species else list(config.SPECIES)
        for key in keys:
            train.train(config.SPECIES[key])
