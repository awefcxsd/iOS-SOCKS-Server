import math
import os
import struct
import wave


class BackgroundAudio:
    """Keep Pythonista's audio session active while the proxy is running."""

    def __init__(self, test_tone=False, frequency=440, audio_path=None):
        self.test_tone = test_tone
        self.frequency = frequency
        filename = "background-tone.wav" if test_tone else "background-silence.wav"
        self.audio_path = audio_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), filename
        )
        self.player = None
        self.audio_session = None
        self.native_session_active = False
        self.session_error = None
        self.error = None

    def _activate_native_audio_session(self):
        try:
            from objc_util import ObjCClass

            audio_session_class = ObjCClass("AVAudioSession")
            self.audio_session = audio_session_class.sharedInstance()
            self.audio_session.setCategory_error_("AVAudioSessionCategoryPlayback", None)
            self.audio_session.setActive_error_(True, None)
            self.native_session_active = True
            return True
        except Exception as error:
            self.session_error = error
            self.audio_session = None
            self.native_session_active = False
            return False

    def _create_audio_file(self):
        if os.path.exists(self.audio_path):
            return

        sample_rate = 8000
        duration_seconds = 1
        sample_count = sample_rate * duration_seconds
        if self.test_tone:
            # Keep the tone quiet enough for testing without being overly disruptive.
            amplitude = int(32767 * 0.1)
            samples = (
                int(amplitude * math.sin(2 * math.pi * self.frequency * i / sample_rate))
                for i in range(sample_count)
            )
            audio = b"".join(struct.pack("<h", sample) for sample in samples)
        else:
            audio = struct.pack("<h", 0) * sample_count

        with wave.open(self.audio_path, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(audio)

    def start(self):
        try:
            import sound
            self._create_audio_file()
            self._activate_native_audio_session()
            self.player = sound.Player(self.audio_path)
            # Player construction may change the shared session configuration.
            self._activate_native_audio_session()
            self.player.number_of_loops = -1
            self.player.play()
        except Exception as error:
            self.error = error
            self.player = None
            return False

        return True

    def stop(self):
        if self.player is not None:
            self.player.stop()
            self.player = None
        if self.native_session_active:
            self.audio_session.setActive_error_(False, None)
            self.native_session_active = False
            self.audio_session = None
