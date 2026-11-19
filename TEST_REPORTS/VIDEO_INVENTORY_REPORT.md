# Video inventory

The original supplied project extraction was used read-only. FFprobe inspected 32 media entries, of which 14 exposed a video stream and decoded an initial frame. Eighteen 16-byte historical uploads or placeholders failed probing. This records rejected files, without attributing the reason for their creation. The two configured raw camera recordings were missing. Valid entries include historical annotations, preview outputs and byte-identical copies. SHA-256 in the inventory distinguishes duplicates. Duration, dimensions, nominal frame rate and frame count are stream metadata. Complete decoding was not performed for every historical output.

The [inventory](VIDEO_INVENTORY.csv) distinguishes successful initial decoding, rejects and blocked configured paths. The short detector/tracker clips were decoded after generation. No labeled detection or physical identity ground truth was available.
