"""Telegram auto-forwarder that runs as your own account (userbot).

Watches source chats/channels and copies or forwards every new post to
target chats, following the rules in config.yaml.

Usage:
    python forwarder.py login        # first run: log in with your phone number
    python forwarder.py chats        # list your chats with their IDs
    python forwarder.py run          # start forwarding (default)
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError

log = logging.getLogger("forwarder")


@dataclass
class Rule:
    name: str
    sources: list
    targets: list
    mode: str = "copy"  # "copy" (no 'Forwarded from' header) or "forward"
    include: list = field(default_factory=list)  # keep only posts matching any of these
    exclude: list = field(default_factory=list)  # drop posts matching any of these
    media_only: bool = False
    text_only: bool = False
    replace: dict = field(default_factory=dict)  # text replacements, copy mode only
    append: str = ""  # text added at the end, copy mode only
    source_ids: set = field(default_factory=set)
    target_entities: list = field(default_factory=list)

    def matches(self, text, has_media):
        if self.media_only and not has_media:
            return False
        if self.text_only and has_media:
            return False
        lowered = (text or "").lower()
        if self.include and not any(_hit(p, lowered) for p in self.include):
            return False
        if any(_hit(p, lowered) for p in self.exclude):
            return False
        return True

    def rewrite(self, text):
        if self.mode != "copy" or text is None:
            return text
        for old, new in self.replace.items():
            text = text.replace(old, new)
        if self.append:
            text = f"{text}\n\n{self.append}" if text else self.append
        return text


def _hit(pattern, lowered_text):
    """Plain keyword (case-insensitive) or regex when written as /pattern/."""
    if len(pattern) > 2 and pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], lowered_text, re.IGNORECASE) is not None
    return pattern.lower() in lowered_text


def load_rules(path):
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rules = []
    for i, raw in enumerate(data.get("rules", [])):
        rule = Rule(
            name=raw.get("name", f"rule-{i + 1}"),
            sources=_as_list(raw.get("sources") or raw.get("source")),
            targets=_as_list(raw.get("targets") or raw.get("target")),
            mode=raw.get("mode", "copy"),
            include=_as_list(raw.get("include")),
            exclude=_as_list(raw.get("exclude")),
            media_only=bool(raw.get("media_only", False)),
            text_only=bool(raw.get("text_only", False)),
            replace=raw.get("replace") or {},
            append=raw.get("append", "") or "",
        )
        if rule.mode not in ("copy", "forward"):
            sys.exit(f"[{rule.name}] mode must be 'copy' or 'forward'")
        if not rule.sources or not rule.targets:
            sys.exit(f"[{rule.name}] needs at least one source and one target")
        rules.append(rule)
    if not rules:
        sys.exit(f"No rules found in {path}")
    return rules


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def make_client():
    load_dotenv()
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    if not api_id or not api_hash:
        sys.exit("Set API_ID and API_HASH in .env (get them at https://my.telegram.org)")
    session = os.getenv("SESSION", "sessions/me")
    os.makedirs(os.path.dirname(session) or ".", exist_ok=True)
    return TelegramClient(session, int(api_id), api_hash)


async def resolve(client, ref):
    """Accept @username, t.me link, numeric ID, or 'me' (Saved Messages)."""
    if isinstance(ref, str) and re.fullmatch(r"-?\d+", ref.strip()):
        ref = int(ref)
    return await client.get_entity(ref)


async def prepare(client, rules):
    await client.get_dialogs()  # fill the entity cache so numeric IDs resolve
    for rule in rules:
        for src in rule.sources:
            entity = await resolve(client, src)
            rule.source_ids.add(await client.get_peer_id(entity))
        for dst in rule.targets:
            entity = await resolve(client, dst)
            if await client.get_peer_id(entity) in rule.source_ids:
                sys.exit(f"[{rule.name}] {dst} is both a source and a target (would loop)")
            rule.target_entities.append(entity)
        log.info("[%s] %d source(s) -> %d target(s), mode=%s",
                 rule.name, len(rule.sources), len(rule.targets), rule.mode)


async def with_flood_retry(coro_factory):
    for _ in range(3):
        try:
            return await coro_factory()
        except FloodWaitError as e:
            log.warning("Flood wait %ss, sleeping...", e.seconds)
            await asyncio.sleep(e.seconds + 1)
    return await coro_factory()


async def send_single(client, rule, target, msg):
    if rule.mode == "forward":
        return await client.forward_messages(target, msg)
    if msg.media and not msg.web_preview:
        return await client.send_file(
            target, msg.media, caption=rule.rewrite(msg.text), parse_mode="md",
        )
    return await client.send_message(
        target, rule.rewrite(msg.text), parse_mode="md", link_preview=bool(msg.web_preview),
    )


async def send_album(client, rule, target, messages):
    if rule.mode == "forward":
        return await client.forward_messages(target, messages)
    captions = [rule.rewrite(m.text) or "" for m in messages]
    return await client.send_file(
        target, [m.media for m in messages], caption=captions, parse_mode="md",
    )


def register_handlers(client, rules):
    def rules_for(chat_id):
        return [r for r in rules if chat_id in r.source_ids]

    async def dispatch(chat_id, text, has_media, sender):
        for rule in rules_for(chat_id):
            if not rule.matches(text, has_media):
                log.debug("[%s] skipped message from %s (filters)", rule.name, chat_id)
                continue
            for target in rule.target_entities:
                try:
                    await with_flood_retry(lambda: sender(rule, target))
                    log.info("[%s] %s -> %s", rule.name, chat_id, getattr(target, "id", target))
                except RPCError as e:
                    log.error("[%s] failed sending to %s: %s", rule.name,
                              getattr(target, "id", target), e)

    @client.on(events.NewMessage())
    async def on_message(event):
        if event.message.grouped_id:
            return  # albums are handled by on_album
        if not rules_for(event.chat_id):
            return
        msg = event.message
        await dispatch(event.chat_id, msg.text, bool(msg.media),
                       lambda rule, target: send_single(client, rule, target, msg))

    @client.on(events.Album())
    async def on_album(event):
        if not rules_for(event.chat_id):
            return
        text = "\n".join(m.text for m in event.messages if m.text)
        await dispatch(event.chat_id, text, True,
                       lambda rule, target: send_album(client, rule, target, event.messages))


async def cmd_run(client, config_path):
    rules = load_rules(config_path)
    me = await client.get_me()
    log.info("Logged in as %s (id %s)", me.first_name, me.id)
    await prepare(client, rules)
    register_handlers(client, rules)
    log.info("Listening for new posts. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


async def cmd_chats(client):
    async for dialog in client.iter_dialogs():
        kind = "channel" if dialog.is_channel and not dialog.is_group else (
            "group" if dialog.is_group else "user")
        username = getattr(dialog.entity, "username", None)
        print(f"{dialog.id:>16}  {kind:<8} {dialog.name}" + (f"  (@{username})" if username else ""))


async def cmd_login(client):
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (id {me.id}). Session saved.")


def main():
    parser = argparse.ArgumentParser(description="Telegram auto-forwarder (userbot)")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "chats", "login"])
    parser.add_argument("-c", "--config", default=os.getenv("CONFIG", "config.yaml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    client = make_client()
    commands = {
        "run": lambda: cmd_run(client, args.config),
        "chats": lambda: cmd_chats(client),
        "login": lambda: cmd_login(client),
    }
    with client:  # starts the client; asks for phone + code on first login
        client.loop.run_until_complete(commands[args.command]())


if __name__ == "__main__":
    main()
