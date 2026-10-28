# systemd units (Linux server deploy)

These four `--user` units run the whole desk off `main`, out of the box. They assume the
checkout lives at `~/desk-repo`; if it doesn't, rewrite the path first:

```bash
sed -i "s#%h/desk-repo#$PWD#g" scripts/systemd/*.service   # from inside your checkout
mkdir -p ~/.config/systemd/user && cp scripts/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now desk-capture desk-strategydriver desk-exitmonitor desk-dashboard
```

Logs: `journalctl --user -u desk-strategydriver -f`. Paper by default — live stays locked
until `LIVE_TRADING_ENABLED=true` + a scoped PASS + wallet keys are set in `~/desk-repo/.env`.
