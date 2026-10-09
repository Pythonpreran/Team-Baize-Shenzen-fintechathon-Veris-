# Landing page demo recording

The player in `#demo` runs a scripted DOM replay of the product by default. Drop a
real screen recording in this folder and it takes over automatically — no code change.

Expected filenames, tried in this order:

| File | Notes |
|---|---|
| `veris-demo.webm` | preferred, VP9 or AV1 |
| `veris-demo.mp4`  | H.264 fallback for Safari |

The `<video>` is `muted loop playsinline`, so it autoplays on every browser. It is
also `preload="none"` until the section scrolls near, which keeps the first paint
cheap. If neither file resolves, the `error` event fires and the scripted replay
stays on screen — the page never shows a broken player.

## Recording notes

- **16:9**, 1600×900 or 1920×1080. The frame is `object-fit: cover`, so anything
  else gets cropped rather than letterboxed.
- Keep it **under ~20 seconds** and loop-friendly: start and end on the same view
  so the wrap is not jarring.
- Four chapters, matching the tab labels. The tabs seek the video to
  `chapter / 4 × duration`, so give each chapter roughly a quarter of the runtime:

  | Tab | What to record |
  |---|---|
  | 01 Read | drag the PDFs in, intake report appears |
  | 02 Grade | findings list with the evidence-class pills |
  | 03 Refuse | the two conflicting endorsements, engine abstains |
  | 04 Route | consent granted for named documents, brief handed over |

- No audio track needed. It is muted and there is no unmute control.

## Compressing

```powershell
ffmpeg -i raw.mp4 -c:v libvpx-vp9 -crf 34 -b:v 0 -an veris-demo.webm
ffmpeg -i raw.mp4 -c:v libx264 -crf 24 -pix_fmt yuv420p -an -movflags +faststart veris-demo.mp4
```

Aim for well under 5 MB. This page is served off `/static`, and a demo machine on
conference wifi should not be waiting on it.
