# Development fixtures

## gettysburg-audio.torrent

A test fixture: the one real multi-file torrent in the suite. The bencode tests parse it,
and the acquisition, import and review suites serve it as the bytes Prowlarr returns so the
fake Deluge can compute the infohash and submit can read the download's shape. It was built
for exercising the compose Deluge without peers (web seeds), which real PandaCD grabs have
covered since M3.

Three public-domain LibriVox recordings of the Gettysburg Address (3.3 MB, MP3 and OGG),
served by archive.org web seeds. Infohash `7838b1f9b3ab320d9e1b265dff0334bcb6238577`.
It needs no peers and completes in seconds, so it exercises the whole add, observe,
complete, move-on-complete path in the compose Deluge.

Built with `scripts/make-fixture-torrent.py` from the audio files of the archive.org item
`gettysburg_shurtagal_librivox`. The item's own `_archive.torrent` is deliberately not used:
archive.org regenerates metadata files after making the torrent, so pieces touching them
never verify and that torrent stalls at roughly 86%. That stall is itself a useful case for
testing stall detection later; fetch it on demand from
`https://archive.org/download/gettysburg_shurtagal_librivox/gettysburg_shurtagal_librivox_archive.torrent`.

The three files are not music, so beets will not tag them as an album. They are for Deluge
mechanics only; import-path tests use a tagged FLAC fixture instead (M4).
