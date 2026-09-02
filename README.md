# StageFront Karaoke Worker v2

Isolated worker for the StageFront Karaoke v2 beta. It polls the protected v2 API, separates songs into vocals and instrumental tracks, transcribes timed lyrics, and renders saved karaoke projects into downloadable MP4 videos.

The worker runs every ten minutes and can also be started manually from GitHub Actions. It contains no credentials; the shared worker key is stored as a GitHub Actions secret.
