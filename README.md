# StageFront Karaoke Worker v2

Isolated worker for the StageFront Karaoke v2 beta. It polls the protected v2 API, separates one queued song into vocals and instrumental tracks with Demucs, and uploads both results to private storage.

The worker runs every ten minutes and can also be started manually from GitHub Actions. It contains no credentials; the shared worker key is stored as a GitHub Actions secret.
