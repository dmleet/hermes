# beets changes needed for Hermes

Reference material to port into the flux repo (`clusters/home/beets`). Hermes never writes
to that repo. Three changes:

1. **Image.** `Dockerfile` here builds on the pinned `lscr.io/linuxserver/beets` digest and
   installs `beets-hermes` into whichever Python environment owns `beet`. The GitHub Actions
   workflow (`.github/workflows/images.yml`) builds and pushes it to Docker Hub on every push
   to `main` as `<namespace>/beets-hermes:<version>-<sha>` (plus `:<version>` and `:latest`),
   alongside `<namespace>/hermes` from the same commit. Point the StatefulSet at the commit tag. The image contract is
   only: `beet` works with `BEETSDIR`, and `hermes-agent` is on PATH. Change the `FROM` line
   to any other beets image (or your own) and nothing else needs to change.
2. **The agent as the pod's process.** `statefulset-patch.yaml` keeps the pod's single
   container but runs `hermes-agent` in it instead of the image's s6 init and web UI (the
   `web` plugin is not used). `/config` (library, config.yaml, state file), `/music` and
   `/downloads` are mounted as before, the container runs as uid 1000 (what PUID/PGID gave
   the old init), the readiness probe becomes the agent's `/healthz`, and port 8337 goes
   away. Manual imports run in the same container as always:
   `kubectl -n beets exec -it beets-0 -- beet import /downloads`. Because the init no longer
   runs, nothing chowns `/config` at start: it must already be owned by uid 1000, which it
   is when the current pod has been writing there as PUID 1000 (`ls -ln /config` to check).
3. **Service.** The StatefulSet already names `serviceName: beets-web` but no Service exists.
   `service.yaml` adds it, name kept, exposing only the agent on 8338.

4. **Config.** `config.yaml` here is the production config with the Hermes changes applied
   and each change marked at the top (`hermes` in, `web` out, `resume: no`, a soundtrack
   path fix, `match.preferred`). The compose stack runs the same file with the slow
   on-import plugins commented out (`dev/beets/config.yaml`), so what the tests exercise
   is what the cluster runs.

The current beets config already satisfies the agent's preconditions (`copy: yes`,
`move: no`, no link/hardlink). `plugins:` must gain `hermes` and can drop `web`, e.g.
`plugins: musicbrainz fetchart ... hermes`. The pinned image ships beets 2.5.1 on
Python 3.12; the plugin's own tests run on 2.13.1, and the agent health check was verified
inside the built image.

Build and smoke-test locally (from the repo root):

```
docker build -f deploy/beets/Dockerfile -t beets-hermes:dev .
docker run --rm --user 1000:1000 --entrypoint sh beets-hermes:dev -c \
  'mkdir -p /tmp/b && printf "plugins: hermes\n" > /tmp/b/config.yaml && BEETSDIR=/tmp/b hermes-agent --help'
```

Import duration: replaygain (ffmpeg, r128 on FLAC), fetchart, embedart and lastgenre all run
on import, so expect minutes per album. Hermes' `beets.import_timeout_seconds` defaults to 30
minutes.
