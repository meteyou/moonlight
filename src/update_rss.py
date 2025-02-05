#!/usr/bin/env python3
# Build RSS feeds from GitHub issues
#
# Copyright (C) 2022  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations
import os
import pathlib
import json
import datetime
import hashlib
import argparse
import email.utils
import sys
import logging
import httpx
import xml.etree.ElementTree as etree
from typing import List, Dict, Any, Optional

GH_HOST = "https://api.github.com"
PARENT_DIR = pathlib.Path(__file__).parent
REQ_CACHE = PARENT_DIR.parent.joinpath("cache/request_cache.json")
ASSETS_PATH = PARENT_DIR.parent.joinpath("assets")
CFG_PATH = PARENT_DIR.joinpath("config.json")
NS_URL = "https://arksine.github.io/moonlight"

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
etree.register_namespace("moonlight", NS_URL)

def RssElement(
    parent: etree.Element,
    tag: str,
    text: Optional[str] = None,
    level: int = 1,
    is_last: bool = False
) -> etree.Element:
    node = etree.SubElement(parent, tag)
    if text is not None:
        node.text = text
    else:
        # assume an rss node with no text has children
        node.text = "\n" + "    " * (level + 1)
    if is_last:
        node.tail = "\n" + "    " * (level - 1)
    else:
        node.tail = "\n" + "    " * level
    return node

class RssDocument:
    def __init__(
        self, name: str,
        options: Dict[str, Any],
        cfg_hash: str
    ) -> None:
        self.name = name
        self.repo = f"{options['repo_owner']}/{options['repo_name']}"
        self.authorized: List[str] = [
            ac.lower() for ac in options["authorized_creators"]
        ]
        self.date = datetime.datetime.now(datetime.timezone.utc)
        self.cfg_hash = cfg_hash
        self.root = etree.Element("rss", {"version": "2.0", "xmlns:moonlight": NS_URL})
        self.root.text = "\n    "
        self.channel = RssElement(self.root, "channel", is_last=True)
        RssElement(self.channel, "title", self.repo.lower(), level=2)
        RssElement(self.channel, "link", f"https://github.com/{self.repo}", level=2)
        RssElement(self.channel, "description", options["description"], level=2)
        date_str = email.utils.format_datetime(self.date, usegmt=True)
        RssElement(self.channel, "pubDate", date_str, level=2)
        RssElement(self.channel, "moonlight:configHash", self.cfg_hash, level=2)

    def add_items_from_issues(self, issues: List[Dict[str, Any]]) -> None:
        for issue in issues:
            user: str = issue["user"]["login"]
            if user.lower() not in self.authorized:
                continue
            item = RssElement(self.channel, "item", level=2)
            RssElement(item, "title", issue["title"], level=3)
            RssElement(item, "link", issue["html_url"], level=3)
            desc: str = issue["body"].strip()
            desc = desc.split("\r\n\r\n", 1)[0]
            desc = desc.replace("\r\n", " ")
            if len(desc) > 512:
                desc = desc[:509] + "..."
            RssElement(item, "description", desc, level=3)
            # Date is in ISO 8601 with a "Z" appended, convert to
            # RFC 2822 format
            date: str = issue["created_at"]
            date = date[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(date)
            rfc_date = email.utils.format_datetime(dt, usegmt=True)
            RssElement(item, "pubDate", rfc_date, level=3)
            priority = "normal"
            labels: List[Dict[str, Any]] = issue["labels"]
            for lbl in labels:
                if lbl["name"] == "critical":
                    priority = "high"
                    break
            RssElement(item, "category", priority, level=3)
            guid = f"{self.repo}/issue/{issue['number']}".lower()
            RssElement(item, "guid", guid, level=3, is_last=True)
        last = list(self.channel)[-1]
        last.tail = last.tail[:-4]

    def equals(self, feed_info: Dict[str, Any]) -> bool:
        other_root: Optional[etree.Element] = feed_info["root"]
        if other_root is None:
            return False
        if self.cfg_hash != feed_info["config_hash"]:
            return False
        cur_items = self.root.findall("channel/item")
        last_items = other_root.findall("channel/item")
        if len(cur_items) != len(last_items):
            return False
        last_uid_map: Dict[str, etree.Element] = {
            node.findtext("guid", default=""): node for node in last_items
        }
        for item in cur_items:
            guid = item.findtext("guid", default="")
            if not guid:
                return False
            last_item = last_uid_map.get(guid)
            if last_item is None:
                return False
            # Compare tags.  There is no need compare guid as we know they
            # match if we have reached this point:
            for tag in ["title", "link", "description", "pubDate", "category"]:
                if item.findtext(tag, default="") != last_item.findtext(tag):
                    return False
        return True

    def write(self):
        tree = etree.ElementTree(self.root)
        path = ASSETS_PATH.joinpath(f"{self.name}.xml")
        tree.write(path, encoding="utf-8", xml_declaration=True)

def hash_config(name: str, options: Dict[str, Any]) -> None:
    hash = hashlib.sha256()
    hash.update(name.encode())
    sorted_opts: Dict[str, Any]
    sorted_opts = dict(sorted(options.items(), key=lambda x: x[0]))
    hash.update(json.dumps(sorted_opts).encode())
    return hash.hexdigest()

def get_feed_info(name: str) -> Dict[str, Any]:
    ret: Dict[str, Any] = {
        "root": None,
        "config_hash": None,
        "last_pub": None
    }
    path = ASSETS_PATH.joinpath(f"{name}.xml")
    if path.is_file():
        et = etree.parse(str(path))
        ret["root"] = et.getroot()
        cfg_hash = et.findtext("channel/moonlight:configHash",
                               namespaces={"moonlight": NS_URL})
        ret["config_hash"] = cfg_hash
        date = et.findtext("channel/pubDate")
        if date is not None:
            try:
                dt = email.utils.parsedate_to_datetime(date)
                ret["last_pub"] = dt
            except Exception:
                pass
    return ret

def read_cache() -> Dict[str, Any]:
    if not REQ_CACHE.is_file():
        return {}
    logging.info("Cache File Found")
    return json.loads(REQ_CACHE.read_text())

def write_cache(data: Dict[str, Any]) -> None:
    if not REQ_CACHE.parent.exists():
        REQ_CACHE.parent.mkdir()
    logging.info("Writing Cache")
    REQ_CACHE.write_text(json.dumps(data))

def read_config() -> Dict[str, Dict[str, Any]]:
    with CFG_PATH.open() as f:
        return json.load(f)

def main(token: Optional[str] = None, enable_cache=False) -> None:
    token = token or os.getenv('GITHUB_TOKEN', None)
    if token is not None:
        logging.info("Github Token Detected")
    force = os.getenv("FORCE_UPDATE", "false").lower() == "true"
    if force:
        logging.info("Force Update Enabled")
    cache: Dict[str, Any] = {}
    if enable_cache:
        cache = read_cache()
    new_cache: Dict[str, Any] = {}
    config = read_config()
    need_commit = False
    for name, options in config.items():
        repo_cache = cache.get(name, {})
        new_cache[name] = dict(repo_cache)
        # Get information about the last feed
        cfg_hash = hash_config(name, options)
        new_cache[name]["config_hash"] = cfg_hash
        feed_info = get_feed_info(name)
        # query issues for each repo, creating xml files.  If
        # a current xml file exists and its contents are the
        # same, don't modify.
        owner = options["repo_owner"]
        repo = options["repo_name"]
        qs = "labels=announcement&per_page=20"
        url = f"{GH_HOST}/repos/{owner}/{repo}/issues?{qs}"
        headers: Dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
        if token is not None:
            headers["Authorization"] = f"token {token}"
        etag = repo_cache.get("etag")
        if etag is not None:
            headers["If-None-Match"] = etag
        with httpx.Client(http2=True) as client:
            resp = client.get(url, headers=headers, timeout=2.0)
        if resp.status_code == 304:
            logging.info(f"Not modified: {name}")
            continue
        elif resp.status_code != httpx.codes.OK:
            logging.info(f"Error fetching {name}")
            continue
        if "etag" in resp.headers:
            new_cache[name]["etag"] = resp.headers["etag"]
        doc = RssDocument(name, options, cfg_hash)
        issues: List[Dict[str, Any]] = resp.json()
        doc.add_items_from_issues(issues)
        if not doc.equals(feed_info) or force:
            need_commit = True
            doc.write()
    if new_cache != cache and enable_cache:
        write_cache(new_cache)
    if need_commit:
        print("commit")
    else:
        print("skip")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Moonlight - RSS Feed Generator")
    parser.add_argument(
        "-t", "--token", default=None, metavar="<token>",
        help="GitHub Authentication Token")
    parser.add_argument(
        "-c", "--cache", action="store_true",
        help="Enable Etag Cache"
    )
    args = parser.parse_args()
    main(args.token, args.cache)
