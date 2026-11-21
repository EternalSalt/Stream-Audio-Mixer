# Salty's Stream Mixer (Windows 10/11)

A per-app audio mixer. Every channel plays to **two outputs at once** (e.g. headphones + speakers),
and you **drag apps between channels** to choose where each app plays.

## First-time setup
1. Install **VB-Cable** from https://vb-audio.com/Cable/ and restart your PC.
   Each channel needs its own cable. The free download gives you one; the A+B / C+D packs add more.
2. Install Salty's Stream Mixer from the latest release (Setup.exe) or run the Portable.exe.
3. On first launch the app assigns a cable to each channel, sets Output 1 to your normal speakers/headphones,
   and makes the first channel your **Windows default output** (gold outline).

## Using it
- **Windows default output (★):** everything on your PC plays through this channel. Click the star button
  on another channel to switch. When you close the app, your original Windows output is restored.
- **Apps:** each channel lists the apps playing through it. Drag an app to another channel to move it.
- **Outputs:** pick two real devices per channel. Each has its own volume and mute.
- **+ Add channel** adds more channels (each needs a free virtual cable).

## If sound ever goes missing
Re-open the app (it restores your output when closed normally), or choose your speakers in
Windows Settings > System > Sound.

## For developers
`pip install -r requirements.txt`, put `SoundVolumeView.exe` next to the script, run `python saltys_stream_mixer.py`.
Config + crash log: `%APPDATA%\SaltysStreamMixer\`.
