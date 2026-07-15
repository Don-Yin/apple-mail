<a href="https://github.com/openclaw"><img src="assets/openclaw.png" alt="OpenClaw" height="60"></a>

[中文文档](README.zh-CN.md)

# apple-mail

![apple-mail](assets/social-card.png)

an agent skill that gives AI assistants full control over macOS Mail.app. triage your inbox, draft replies, search, move, delete, and label emails - all through natural language.

## install

```bash
# cursor
git clone https://github.com/Don-Yin/apple-mail.git .cursor/skills/apple-mail

# claude code
git clone https://github.com/Don-Yin/apple-mail.git .claude/skills/apple-mail

# openclaw
git clone https://github.com/Don-Yin/apple-mail.git .openclaw/skills/apple-mail
```

requires macOS with Mail.app configured, [micromamba](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html), and full disk access for Terminal. on first run, `mail.sh` bootstraps a Python 3.11 environment automatically.

## how it works

- reads `.emlx` files directly from disk (~5 ms per message vs seconds via scripting bridge)
- falls back to JXA when disk access misses
- maintains a SQLite FTS5 index for fast full-text search
- confirmed deletion moves mail to the account's Trash/Deleted Items instead of using Mail's crash-prone delete verb
- optional Exchange server drafting uses an explicit protocol-v1 adapter and is disabled by default
- verified Exchange drafts can request Arial 11pt with `--font arial` or `APPLE_MAIL_DRAFT_FONT=arial`
- nothing sends without explicit user confirmation

The public repository ships no Exchange authentication adapter. See `references/tool-reference.md` for the stdin JSON contract and configuration variables.

full command reference in `SKILL.md` and `references/tool-reference.md`.

## acknowledgements

the disk-first reading approach and FTS5 search index design were inspired by [imdinu/jxa-mail-mcp](https://github.com/imdinu/jxa-mail-mcp).

## license

MIT
