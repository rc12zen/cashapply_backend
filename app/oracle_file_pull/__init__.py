"""
app.oracle_file_pull
=======================
Pulls files from the Oracle Cloud file-transfer VM (144.24.100.229,
"zenappdev") down to this app's local watch folders, over the confirmed
two-hop SSH jump chain (App VM -> DMZ 192.168.7.30 -> Oracle Cloud VM).

This is a SEPARATE, new piece — it does not change how the aging/GL-rates
watchers themselves work (app.aging.watcher / app.gl_rates.watcher). Its
entire job is: check the remote files' mtimes, and if changed, SFTP them
down into the same local folders those watchers already poll. So the
ingestion side (parsing, DB upsert, etc.) needs zero new code — only this
pull step feeds it.

See puller.py for the actual implementation.
"""