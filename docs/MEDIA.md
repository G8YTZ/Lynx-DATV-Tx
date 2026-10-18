# Preparing Media for Lynx-Tx in DaVinci Resolve

## The house standard

Every file played out by Lynx-Tx should be **3840×2160, 25p, H.265 Main (8-bit), Rec.709 standard range, with AAC 48 kHz stereo audio**. Lynx-Tx re-encodes it to about 3.8 Mb/s for transmission, so the master only needs to be clean and correct, not small.

What goes wrong when a file is off-standard:

- **Other frame rates** (24, 30, 60) judder against the 25p transmission.
- **HDR** (HLG, PQ, Dolby Vision) looks flat and washed out on the standard-range picture that goes on air.
- **10-bit** (H.265 Main 10) is harder for the ROCK's hardware decoder and gains nothing, because transmission is 8-bit.
- **1080p** works, but should be upscaled to 3840×2160 in Resolve rather than left to the transmitter.

## 1. Start from the camera originals

Most problems start before Resolve, with a copy that is not the original. Check each source clip's resolution and frame rate before editing.

- **iPhone: shoot at 25 fps.** 25 fps only appears with **Settings → Camera → Record Video → Show PAL Formats** switched on. The iPhone 15 Pro offers 24, 25, 30 and 60, but not 50.
- **Check on the phone.** In Photos, swipe up on a clip: the panel shows its resolution and frame rate as recorded.
- **Avoid Photos "Memories" movies.** Photos renders them at 30 fps, whatever the clips were shot at. Import the individual clips instead.
- **Transfer the originals.** Use AirDrop with **Options → All Photos Data** on, or connect the phone and use **Image Capture** on the Mac. Some sharing routes quietly re-encode to a lower resolution or a different frame rate.
- **Sony ZV-1 and other cameras:** set the camera to 25p (PAL) before shooting.

## 2. Project settings (set once)

Set these once, save them as your default, and every new project starts ready. Open **File → Project Settings** (the cog, bottom right).

**Master Settings**

- Timeline resolution: **3840 × 2160**
- Timeline frame rate: **25**
- Frame Interpolation → Retime process: **Optical Flow**
- Frame Interpolation → Motion estimation mode: **AI Speed Warp Better** (Studio), or **Enhanced Better**

Optical flow builds smooth in-between frames when a clip's frame rate differs from 25. The default, *Nearest*, drops frames instead and stutters on movement. It makes rendering slower, so give clips the right frame rate in the camera wherever possible.

**Color Management**

- Color science: **DaVinci YRGB Color Managed**
- Automatic color management: **on**
- Color processing mode: **SDR**
- Output color space: **SDR Rec.709**

Resolve then reads each clip's own colour tags and converts HDR footage (iPhone HLG and Dolby Vision) to standard range automatically. Do not also add a Color Space Transform node by hand, or the clip is converted twice and comes out too dark and saturated.

**Optional: Broadcast Safe.** Set IRE levels to **−10 to 110** and tick **Make broadcast safe** to clip stray super-whites.

**Save as default.** Click **•••** at the top right of Project Settings → **Save Current Settings as Preset**, then **Save As User Default Config**.

## 3. Upscaling 1080p footage

On a 3840×2160 timeline, Resolve scales 1080p clips up automatically. For the best result, use Super Scale (DaVinci Resolve Studio) on each 1080p clip.

1. In the media pool, right-click the clip → **Clip Attributes**.
2. Super Scale: **2× Enhanced**.
3. Noise reduction: about **0.65**. Sharpness: about **0.3**.
4. Render a 10-second test of the busiest part (faces, coloured light, haze) and judge it full screen at normal viewing distance.

Why those values: grain and over-sharpened edges are random detail the transmitter's encoder cannot predict, so they waste bits at 3.8 Mb/s. A slightly clean, slightly soft master looks better on air. If faces look waxy, ease noise reduction back towards 0.5. If Enhanced looks over-processed, use plain 2×.

Without Studio, set **Project Settings → Image Scaling → Resize filter** to **Sharper**.

Upscaled 1080p looks like 4K but carries no extra detail. For the best picture on HD-only receivers, a native 1080p25 master is also a valid choice.

## 4. The render preset (Deliver page)

Set these once on the Deliver page and save them as a preset; after that, every master is one click.

| Tab      | Setting                            | Value                             |
|----------|------------------------------------|-----------------------------------|
| Video    | Format                             | MP4                               |
| Video    | Codec                              | H.265                             |
| Video    | Use hardware acceleration          | On                                |
| Video    | Resolution                         | Timeline Resolution (3840 × 2160) |
| Video    | Frame rate                         | Timeline Frame Rate (25)          |
| Video    | Chapters from Markers              | **Off**                           |
| Video    | Quality                            | Restrict to **60,000 Kb/s**       |
| Video    | Encoding Profile                   | **Main** (not Main 10)            |
| Video    | Key Frames                         | Automatic                         |
| Advanced | Data Levels                        | Auto                              |
| Advanced | Retain sub-black and super-white   | Off                               |
| Advanced | Color Space Tag / Gamma Tag        | Same as project                   |
| Advanced | **Bypass re-encode when possible** | **Off**                           |
| Audio    | Codec                              | AAC                               |
| Audio    | Bit rate                           | 320 Kb/s, constant                |
| Audio    | Sample rate                        | 48,000 Hz                         |
| Audio    | Output track                       | Bus 1 (Stereo)                    |
| File     | Render without timecode            | **On**                            |

Three settings matter most:

- **Bypass re-encode off.** Left on, Resolve can copy a clip's original compressed video straight into the export, keeping its 10-bit depth and odd frame rate.
- **Main profile.** This is what makes the file 8-bit.
- **No chapters, no timecode.** Both add extra tracks to the MP4 that nothing on air uses.

**Save it.** In the Render Settings panel, **••• → Save As New Preset**, named e.g. *Lynx-Tx 4K25 Master*. It then appears in the **Preset** drop-down in every project. To share it, use **••• → Export Preset**; others import it with **••• → Import Preset**.

## 4b. HD masters for Lynx-Deck

[Lynx-Deck](https://github.com/G8YTZ/lynx-deck) v1 (Pi 4), the Lynx family's HyperDeck-style media player, plays out 1080p over HDMI, so it gets its own HD master. Render it from the same 3840×2160 timeline: Resolve scales the full 4K picture down, which looks sharper than editing in HD.

| Setting                              | 4K master (Lynx-Tx)          | HD master (Lynx-Deck)                        |
|--------------------------------------|------------------------------|----------------------------------------------|
| Resolution                           | 3840 × 2160                  | **1920 × 1080** (timeline stays 3840 × 2160) |
| Frame rate                           | 25                           | 25                                           |
| Codec                                | H.265, Main profile          | **H.264, High profile**                      |
| Quality                              | Restrict to 60,000 Kb/s      | Restrict to **20,000 Kb/s**                  |
| Audio                                | AAC 320 Kb/s, 48 kHz, stereo | same                                         |
| Bypass re-encode, chapters, timecode | off, off, off                | same                                         |

- **25p, not 50p.** Lynx-Deck outputs 1080p50 over HDMI and shows each frame twice, so 25p content plays smoothly.
- **H.264.** The Pi 4 decodes both H.264 and H.265 in hardware; H.264 at 1080p is the safer default.

**Save it as a second preset.** Starting from the 4K preset, change the four settings above and use **••• → Save As New Preset**, named e.g. *Lynx-Deck HD25 Master*.

**Render both in one go.** On the Deliver page, pick the 4K preset → **Add to Render Queue**, then the HD preset → **Add to Render Queue**, then **Render All**.

Upload HD masters through Lynx-Deck's own web page (port 8080 on the Deck), not the Lynx-Tx Media page.

## 5. Check the finished file

The easiest check is to upload the file through the **Lynx-Tx Media page** (`http://<rock address>:8080/media`). It shows each file's resolution, frame rate, bit depth and range, with anything off-standard in amber.

Or check on the Mac with ffprobe (`brew install ffmpeg`):

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=profile,pix_fmt,width,height,r_frame_rate,color_transfer "My Master.mp4"
```

| Field          | Expected    | If it differs                                                |
|----------------|-------------|--------------------------------------------------------------|
| profile        | Main        | Main 10: set Encoding Profile to Main                        |
| pix_fmt        | yuv420p     | yuv420p10le: 10-bit, same fix                                |
| width × height | 3840 × 2160 | check the timeline resolution                                |
| r_frame_rate   | 25/1        | 25000/1001 or other: check the timeline and source clips     |
| color_transfer | bt709       | arib-std-b67 (HLG) or smpte2084 (PQ): check Color Management |

Finally, watch some movement at full screen. Smooth motion means the retiming worked; a regular stutter means Retime process is still on Nearest.

## Checklist for each master

- [ ] Source clips are camera originals, ideally shot at 25p
- [ ] Project timeline is 3840 × 2160 at 25 fps
- [ ] Color Management is Color Managed, SDR, Rec.709 output
- [ ] Retime process is Optical Flow
- [ ] 1080p clips use Super Scale 2× Enhanced (NR ≈ 0.65, sharpness ≈ 0.3)
- [ ] Rendered with the *Lynx-Tx 4K25 Master* preset
- [ ] HD version rendered with the *Lynx-Deck HD25 Master* preset, if it is going on Lynx-Deck
- [ ] Checked: Main, yuv420p, 3840 × 2160, 25/1, bt709
- [ ] Uploaded through the Lynx-Tx Media page (4K) and Lynx-Deck's web page (HD), all green

## Glossary

The terms behind the settings above, and some you will meet in camera menus, TV specifications and other people's files. Grouped by topic, so it can be read straight through.

### Codecs and profiles

- **Codec**: Short for coder-decoder. The method used to compress video for storage or transmission, and to decompress it again for display.
- **H.264 (AVC)**: The codec of HD television, Blu-ray and most of the internet since 2003. Its common profiles are Baseline, Main and High, in rising order of efficiency.
- **H.265 (HEVC)**: High Efficiency Video Coding, its successor from 2013. It needs roughly half the bitrate of H.264 for the same picture, which is what makes 4K possible at amateur bitrates. Lynx-Tx transmits H.265.
- **Profile**: The set of coding tools an encoder may use, and so the set a decoder must support. A file is only playable if the receiver supports its profile.
- **Main**: The basic H.265 profile: 8-bit, 4:2:0. Almost every H.265 decoder handles it, which is why it is the house standard.
- **Main 10**: H.265 with 10-bit samples. It is required for HDR, but many older and cheaper decoders cannot play it, and the Lynx-Tx media player refuses it. A Main 10 decoder also plays Main; not the other way round.
- **Level**: The ceiling on resolution, frame rate and bitrate within a profile. 4K at 25p needs H.265 Level 5.0; 4K at 50p needs 5.1.
- **Tier**: H.265's two bitrate ceilings within a level. Main tier covers everything here; High tier is for studio work.
- **GOP**: Group of pictures: A keyframe and the frames that depend on it. Longer GOPs save bits but take longer to recover from errors and to start decoding.
- **I-frame (keyframe)**: A complete picture that decodes on its own. Receivers can only start, or recover, at one.
- **P-frame and B-frame**: Pictures that store only the differences from earlier frames (P) or from frames on both sides (B). They are far smaller than I-frames.
- **CBR and VBR**: Constant and variable bitrate. A transmitter needs CBR, because the channel carries a fixed number of bits every second; files can be VBR.

### Containers and streams

- **Container**: The wrapper that holds the coded video, audio and timing together. The codec is what is inside; the container is the box.
- **MP4 and MOV**: File containers for storage and editing. Resolve renders to either.
- **MKV**: Matroska, a flexible open container common on the internet.
- **MPEG transport stream (TS)**: The broadcast container: Fixed 188-byte packets designed to survive a noisy channel. DVB, and therefore every DATV transmission, uses it. Lynx-Tx repackages files into it.
- **PID**: Packet identifier: The number labelling each stream inside a transport stream, such as video on 0x101 and audio on 0x102.
- **PCR**: Programme clock reference: The timestamps that let a receiver lock its clock to the transmitter's, so pictures play smoothly with sound in sync.

### Bit depth and colour sampling

- **Bit depth**: The number of levels each sample can take. 8-bit gives 256 levels per component; 10-bit gives 1,024.
- **Banding**: Visible steps in smooth gradients, such as a sky, when there are too few levels. 10-bit reduces it; good 8-bit encoding with gentle grading keeps it out of sight.
- **Luma and chroma (Y′CbCr)**: Video is stored as brightness (luma, Y′) and two colour-difference signals (chroma, Cb and Cr), not as red, green and blue. The eye is far more sensitive to brightness detail than to colour detail.
- **Chroma subsampling (4:4:4, 4:2:2, 4:2:0)**: How much colour detail is kept. 4:4:4 keeps all of it; 4:2:2 halves it horizontally; 4:2:0 halves it both ways. Television transmission is 4:2:0; many HDMI cameras deliver 4:2:2.
- **Video range and full range**: In 8-bit video, black is 16 and peak white 235, leaving headroom either side: Video (legal, or broadcast) range. Computers use full range, 0 to 255. A mismatch makes pictures look washed out or crushed.
- **Broadcast safe**: Keeping levels and colours inside the legal range, as Resolve's "Make broadcast safe" option does.

### Colour standards

- **ITU-R and "Rec."**: The ITU's Radiocommunication Sector writes the international television standards. "Rec." means Recommendation, so Rec.709 is the same thing as ITU-R BT.709.
- **Colour space**: The full definition of how numbers become colours: The primaries, the white point and the transfer function.
- **Primaries and gamut**: The exact red, green and blue a standard uses. Together they define the gamut, the range of colours that can be shown.
- **White point (D65)**: The colour of "white". D65, a standard daylight, is used by every television standard here.
- **Transfer function (gamma)**: The curve linking the stored numbers to the light on screen. It spends more of the levels in the shadows, where the eye is most sensitive.
- **Rec.601 (BT.601)**: The colour standard for standard-definition television, 576 and 480 lines.
- **Rec.709 (BT.709)**: The standard for HD, first published in 1990. It defines the primaries, white point and coding that almost every SDR picture uses today, 4K included. The house standard.
- **BT.1886 (gamma 2.4)**: How a reference display should turn Rec.709 signals into light, assuming a dim viewing room. This is the "Gamma 2.4" in the export settings.
- **sRGB**: The computer and web standard. It shares Rec.709's primaries but assumes a brighter room, with a gamma of about 2.2.
- **DCI-P3 and Display P3**: The digital cinema gamut and Apple's version of it. Wider than Rec.709, narrower than Rec.2020.
- **Rec.2020 (BT.2020)**: The UHD standard from 2012, with a much wider gamut and 10- or 12-bit coding. In practice it travels with HDR.

### HDR

- **SDR**: Standard dynamic range: Conventional television, mastered for a peak around 100 nits. Everything Lynx-Tx sends is SDR.
- **HDR**: High dynamic range: Brighter highlights and deeper shadow detail, needing 10-bit coding and a display that supports it. On a display that does not, HDR looks dull and washed out.
- **Nit**: One candela per square metre, the unit of screen brightness. SDR is mastered at about 100 nits; HDR highlights reach 1,000 or more.
- **Rec.2100 (BT.2100)**: The ITU's HDR standard. It uses Rec.2020 colour and allows two transfer functions, PQ and HLG.
- **PQ (SMPTE ST 2084)**: Perceptual quantiser, from Dolby: An absolute curve in which each code value means a specific brightness, up to 10,000 nits.
- **HLG**: Hybrid log-gamma, developed by the BBC and NHK for broadcasting. A relative curve with no metadata, so the same signal looks reasonable on both SDR and HDR sets. It is used by iPlayer and much live HDR sport.
- **HDR10**: The open, baseline HDR format: PQ, Rec.2020, 10-bit, and static metadata describing the mastering display (SMPTE ST 2086) and the brightest content (MaxCLL, MaxFALL).
- **HDR10+**: HDR10 with dynamic metadata, which adjusts tone mapping scene by scene. Led by Samsung, royalty-free.
- **Dolby Vision**: Dolby's licensed HDR system, with dynamic metadata and up to 12-bit. It comes in several profiles, some built on an HDR10 base so that other sets can still play them. Phones increasingly record it, which is one reason the guide insists on converting to Rec.709.
- **Metadata, static and dynamic**: Information carried alongside HDR pictures telling the display how they were mastered. Static metadata covers the whole programme; dynamic changes scene by scene.
- **Tone mapping**: Squeezing a picture's brightness range into what a display can show, for example HDR into SDR. Done badly, it gives grey blacks and flat colour.

### Picture format and timing

- **UHD and 4K**: Consumer "4K" is UHD, 3840×2160, exactly four times HD. Cinema's DCI 4K is slightly wider, 4096×2160.
- **HD**: 1920×1080 (Full HD), or 1280×720.
- **SD**: Standard definition, 720×576 in Europe.
- **Progressive and interlaced (p and i)**: Progressive sends complete frames; interlaced sends alternate lines in two fields per frame. 25p is 25 complete frames a second; 50i is 50 fields, making 25 frames.
- **25p and 50p**: The European frame rates, rooted in the 50 Hz mains. The house standard is 25p, to match the camera and halve the bitrate of 50p.
- **23.976, 29.97 and 59.94**: Film and North American rates. Converting them to 25p means repeating or blending frames, which shows as judder, so set the timeline frame rate before editing.
- **Pixel aspect ratio**: Whether pixels are square. HD and UHD use square pixels; SD did not.

### Audio

- **AAC**: Advanced Audio Coding, the usual audio codec alongside H.264 and H.265. AAC-LC, the low-complexity version, is the one used here.
- **MPEG-1 Layer II (MP2)**: The older broadcast audio codec of early DVB and many DATV systems.
- **Sample rate (48 kHz and 44.1 kHz)**: 48 kHz is the broadcast and video standard; 44.1 kHz comes from CD. Mixing the two needs resampling, so keep everything at 48 kHz.
- **LUFS and EBU R128**: Loudness units relative to full scale, a measure of perceived loudness. EBU R128 sets broadcast programme loudness at −23 LUFS, so that programmes match without anyone reaching for the volume control.

Justin Cockett, G8YTZ
