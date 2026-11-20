# SonarLite (Windows 10/11)

Sonar-style mixer: channels, drag & drop apps between channels, two outputs per channel.

## One-time setup
1. Install Python 3.10+ and run:  `pip install -r requirements.txt`
2. Download **SoundVolumeView** (free, NirSoft) and put `SoundVolumeView.exe` next to `sonarlite.py`:
   https://www.nirsoft.net/utils/sound_volume_view.html
3. Install virtual audio cables. Each channel needs its OWN cable:
   - VB-Cable (free, 1 cable): https://vb-audio.com/Cable/
   - More cables: VB-Cable A+B / C+D packs (donationware), or Voicemeeter's virtual inputs.
4. Run:  `python sonarlite.py`

## Using it
- On each channel open **Virtual device setup**:
  - *Apps play to* = the cable's playback device (e.g. "CABLE Input")
  - *Read from* = the same cable's recording side (e.g. "CABLE Output")
- Pick **Output A / Output B** (real headphones, speakers, stream mix device, etc.). Never pick the cable itself.
- Drag apps from "Other apps" onto a channel, or between channels. Drop back on "Other apps" to unroute.
- Assignments are remembered and re-applied when an app restarts.
- **+ Add channel** adds more (limited by how many virtual cables you have installed).

## Notes
- Per-app routing needs Windows 10 1803+ / Windows 11. Some apps (browsers, Discord) only list
  once they've played sound; a few need pausing/replaying for the switch to take effect.
- Config is stored in %APPDATA%\SonarLite\config.json.
- Build an .exe: `pip install pyinstaller` then `pyinstaller --noconsole --onefile sonarlite.py`
  (keep SoundVolumeView.exe beside it).
