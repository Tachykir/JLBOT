# minitouch (prebuilt)

Źródło: pakiet npm `@devicefarmer/minitouch-prebuilt` 1.3.2
(https://github.com/devicefarmer/minitouch), licencja Apache-2.0 (plik LICENSE).
Pliki skopiowane bez zmian z `prebuilt/<abi>/bin/minitouch`.

Bot wgrywa plik pasujący do `ro.product.cpu.abi` emulatora do
`/data/local/tmp/minitouch` i uruchamia go przez `su` (Android 10+
bez roota nie daje dostępu do /dev/input).

SHA-256:
```
0dc348ee8e2b1b06ad44390b8a6975649f7d1ec63e9221b989b4e9ffc8996efb  arm64-v8a/minitouch
b99f0440a1f1a4e10a72f315f41112c3afd8dd07f9c2009c2c673ad5fc83035b  x86/minitouch
dd88e8c80f9d7ad996785f93133c7c4273804206a423cdff25056358aaaeb532  x86_64/minitouch
```
