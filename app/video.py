import json
import subprocess
from fractions import Fraction
from pathlib import Path


class VideoError(Exception):
    pass


def probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                           capture_output=True, timeout=45, check=True)
        data = json.loads(r.stdout)
        stream = next(s for s in data["streams"] if s["codec_type"] == "video")
        width, height = int(stream["width"]), int(stream["height"])
        rotation = next((x.get("rotation", 0) for x in stream.get("side_data_list", []) if "rotation" in x),
                        stream.get("tags", {}).get("rotate", 0))
        if abs(int(rotation)) % 180 == 90:
            width, height = height, width
        duration = float(data["format"].get("duration") or stream.get("duration") or 0)
        fps = float(Fraction(stream.get("avg_frame_rate", "0/1")))
        return {"width": width, "height": height, "duration": duration, "fps": fps,
                "codec": stream["codec_name"], "pixel_format": stream.get("pix_fmt"),
                "audio_codec": next((s["codec_name"] for s in data["streams"] if s["codec_type"] == "audio"), None)}
    except FileNotFoundError:
        raise VideoError("FFmpeg is missing. Run the Docker Compose version, which includes it.") from None
    except (subprocess.SubprocessError, ValueError, KeyError, StopIteration, ZeroDivisionError):
        raise VideoError("This file could not be read as a video. Try a valid MP4, MOV, or WebM.") from None


def prepare(source, destination, platforms):
    info = probe(source)
    if not 3 <= info["duration"] <= 600:
        raise VideoError("Use a video between 3 seconds and 10 minutes long.")
    if min(info["width"], info["height"]) < 360 or max(info["width"], info["height"]) > 4096:
        raise VideoError("Video dimensions must be between 360 and 4096 pixels.")
    if "youtube" in platforms and (info["duration"] > 180 or info["width"] > info["height"]):
        raise VideoError("YouTube Shorts needs a square or vertical video up to 3 minutes. Deselect Shorts to upload this video elsewhere.")
    if "instagram" in platforms and info["duration"] > 180:
        raise VideoError("For this cross-posting workflow, use an Instagram Reel up to 3 minutes long.")
    # Remux compatible MP4s; normalize MOV/WebM/codecs for consistent cross-platform acceptance.
    compatible = (info["codec"] == "h264" and info["pixel_format"] == "yuv420p"
                  and info["audio_codec"] in (None, "aac") and 23 <= info["fps"] <= 60
                  and info["width"] % 2 == 0 and info["height"] % 2 == 0)
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?"]
    if compatible:
        args += ["-c", "copy"]
    else:
        args += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
        if not 23 <= info["fps"] <= 60:
            args += ["-r", "30"]
    args += ["-movflags", "+faststart", str(destination)]
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=1800)
    except (subprocess.SubprocessError, OSError):
        Path(destination).unlink(missing_ok=True)
        raise VideoError("Video preparation failed. Try exporting an H.264 MP4 with AAC audio.") from None
    return probe(destination)
