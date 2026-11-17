# Silent Forest — Releases

Questo repository ospita i pacchetti pubblici di Silent Forest per Windows e gli strumenti di manutenzione delle release.

## Scaricare il gioco

1. Aprire [Releases](https://github.com/Davhel98/SilentForest_Release/releases).
2. Scaricare l'asset `SilentForest-v<versione>-Win64.zip` della versione desiderata ed estrarlo in una cartella dedicata.
3. Avviare `SilentForest.exe`. I pacchetti della pipeline includono il controllo degli aggiornamenti Stable/Latest, con conferma prima dell'installazione.

Le release **Pre-release** sono build Non-Stable destinate al collaudo. Dopo i test vengono promosse manualmente a Stable da GitHub.

## Cronologia e pulizia

Promuovendo una versione come `1.2.13` a Stable, la workflow **Stable release cleanup** rimuove le altre release `1.2.X` e i loro asset. Le altre serie rimangono disponibili. Le eliminazioni interessano soltanto gli oggetti Release: i tag Git vengono conservati.

Il riepilogo e disponibile in Actions. Per una verifica o un nuovo tentativo, **Run workflow** accetta il release ID della Stable da conservare; `dry_run` e attivo di default.

## Riferimenti alla build

I sorgenti del gioco sono mantenuti nel repository privato del progetto. Il manifest di ciascun pacchetto registra `sourceRepository`, `sourceCommit` e un `sourceTag` distinto per build. La pulizia pubblica non accede al repository privato e conserva la sua cronologia.

I tag `v<versione>` di questo repository identificano la distribuzione pubblica e puntano al suo branch di manutenzione. Gli archivi Source code generati automaticamente da GitHub contengono quindi i file di manutenzione; per giocare usare l'asset Win64 della release.

## Manutenzione

`Scripts/CleanupReleases.py` e i relativi test gestiscono esclusivamente le release GitHub. La workflow usa il token temporaneo del repository con `contents: write`; i giocatori possono scaricare e aggiornare senza credenziali GitHub.

Per consentire la sostituzione delle Non-Stable lasciare disabilitata l'immutabilita delle nuove release durante il collaudo. La pipeline Unreal protegge comunque le versioni gia dichiarate Stable.
