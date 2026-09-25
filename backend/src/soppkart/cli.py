"""Command line entry point: `soppkart {fetch,features,train,all,adduser}`."""

import argparse
import getpass
import logging
import sys

from soppkart import auth, config, features, sources, train, userfindings
from soppkart.grid import Grid


def add_user(username: str, is_admin: bool) -> None:
    """Create a user, asking for the password. The first admin gets old unowned findings.

    For an existing user (e.g. one who registered on the site), sets the new password
    and, with --admin, makes them admin.
    """
    password = getpass.getpass(f"Passord for {username}: ")
    if len(password) < 8:
        sys.exit("Passordet må ha minst 8 tegn")
    if getpass.getpass("Gjenta passord: ") != password:
        sys.exit("Passordene er ulike")
    existing = auth.get_user_by_name(username)
    if existing is not None:
        user = auth.update_user(
            existing.id, is_admin=is_admin or existing.is_admin, password=password
        )
        assert user is not None
        print(f"Oppdaterte {'admin' if user.is_admin else 'bruker'} {user.username} (nytt passord)")
    else:
        user = auth.add_user(username, password, is_admin, set())
        print(f"Opprettet {'admin' if is_admin else 'bruker'} {user.username}")
    if is_admin:
        claimed = userfindings.claim_unowned(user.id)
        if claimed:
            print(f"{claimed} funn uten eier er nå dine")


def main() -> None:
    parser = argparse.ArgumentParser(prog="soppkart", description="Kantarell map pipeline")
    parser.add_argument(
        "step",
        choices=["fetch", "features", "train", "all", "adduser"],
        help="fetch: download source data, features: build feature rasters, "
        "train: train model and write score map, all: everything, "
        "adduser: create a login (asks for the password)",
    )
    parser.add_argument("--username", help="adduser: the new user's username")
    parser.add_argument("--admin", action="store_true", help="adduser: make the user an admin")
    parser.add_argument(
        "--species",
        choices=list(config.SPECIES),
        help="only train this species (default: all)",
    )
    args = parser.parse_args()

    if args.step == "adduser":
        if not args.username:
            parser.error("adduser needs --username")
        add_user(args.username, args.admin)
        return

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
