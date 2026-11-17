#!/usr/bin/env python3
"""Delete old images from the Cloud Run Artifact Registry repo without touching anything Cloud Run can still use.

Registry cleanup policies cannot see Cloud Run: the digest a revision runs is often untagged, so tag-based rules
(or "delete untagged") can delete the live image, and the next cold start then fails. This script protects

* the digest of every revision that receives traffic,
* the digests of the ``--keep-revisions`` newest revisions of each service (rollback),
* the ``--keep-newest`` newest images of each package,

and deletes the rest that is older than ``--older-than-days``. Dry run unless ``--apply`` is given.

    python scripts/cleanup_artifact_registry.py                 # list what would be deleted
    python scripts/cleanup_artifact_registry.py --apply
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone


def gcloud(*args: str) -> list | dict:
    out = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True, check=True).stdout
    return json.loads(out or "[]")


def protected_digests(project: str, region: str, keep_revisions: int) -> set[str]:
    digests: set[str] = set()
    for service in gcloud("run", "services", "list", "--project", project):
        name = service["metadata"]["name"]
        revisions = gcloud("run", "revisions", "list", "--service", name, "--region", region, "--project", project)
        revisions.sort(key=lambda r: r["metadata"]["creationTimestamp"], reverse=True)
        serving = {t["revisionName"] for t in service["status"].get("traffic", []) if t.get("revisionName")}
        for rev in revisions:
            if rev["metadata"]["name"] in serving or rev in revisions[:keep_revisions]:
                digest = rev["status"].get("imageDigest", "")
                if "@" in digest:
                    digests.add(digest.split("@", 1)[1])
    return digests


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="apps-aecs4u")
    ap.add_argument("--region", default="europe-west1")
    ap.add_argument("--repo", default="cloud-run-source-deploy")
    ap.add_argument("--older-than-days", type=int, default=14)
    ap.add_argument("--keep-newest", type=int, default=3, help="newest images kept per package")
    ap.add_argument("--keep-revisions", type=int, default=3, help="newest revisions per service kept for rollback")
    ap.add_argument("--apply", action="store_true", help="delete (default: only list)")
    args = ap.parse_args()

    keep = protected_digests(args.project, args.region, args.keep_revisions)
    if not keep:
        print("No live digests found; refusing to continue.", file=sys.stderr)
        return 1
    root = f"{args.region}-docker.pkg.dev/{args.project}/{args.repo}"
    images = gcloud("artifacts", "docker", "images", "list", root, "--include-tags")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)

    by_package: dict[str, list] = {}
    for img in images:
        by_package.setdefault(img["package"], []).append(img)
    def created_at(img: dict) -> datetime:
        return datetime.fromisoformat(img["createTime"].replace("Z", "+00:00"))

    doomed = []
    for package, items in by_package.items():
        items.sort(key=lambda i: i["createTime"], reverse=True)
        # One push stores several entries (image index, per-platform image, attestation) within a second or two, and
        # only one of them is the digest Cloud Run reports: keep the whole push of every protected digest.
        kept_pushes = [created_at(i) for i in items if i["version"] in keep]
        for img in items[args.keep_newest :]:
            created = created_at(img)
            same_push = any(abs((created - k).total_seconds()) <= 30 for k in kept_pushes)
            if img["version"] not in keep and not same_push and created < cutoff:
                doomed.append(img)

    print(f"{len(images)} images, {len(keep)} protected digests, {len(doomed)} to delete (older than "
          f"{args.older_than_days} days)")
    for img in doomed:
        print(("DELETE " if args.apply else "would delete ") + f"{img['package']}@{img['version']} {img['createTime']}")
        if args.apply:
            subprocess.run(
                ["gcloud", "artifacts", "docker", "images", "delete", f"{img['package']}@{img['version']}",
                 "--delete-tags", "--quiet", "--project", args.project],
                check=False,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
