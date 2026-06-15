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
        self.background_task = None
        self.player_backend = None
        self.player_error = None
        self.audio_session = None
        self.native_session_active = False
        self.host_supports_background_audio = None
        self.session_error = None
        self.error = None

    def _start_pyto_background_task(self):
        import background

        background_task_class = getattr(background, "BackgroundTask", None)
        if background_task_class is None:
            raise ImportError("background.BackgroundTask is unavailable")

        self.background_task = background_task_class(audio_path=self.audio_path)
        self.background_task.reminder_notifications = False
        self.background_task.start()
        self.host_supports_background_audio = True
        self.player_backend = "Pyto BackgroundTask"

    def _check_host_background_audio_mode(self):
        try:
            from objc_util import ObjCClass

            info = ObjCClass("NSBundle").mainBundle().infoDictionary()
            modes = info.objectForKey_("UIBackgroundModes")
            self.host_supports_background_audio = bool(
                modes and any(str(mode) == "audio" for mode in modes)
            )
        except Exception:
            self.host_supports_background_audio = None

    def _activate_native_audio_session(self):
        try:
            from objc_util import ObjCClass

            audio_session_class = ObjCClass("AVAudioSession")
            self.audio_session = audio_session_class.sharedInstance()
            # Match Pyto's BackgroundTask: playback bypasses the silent switch,
            # mixWithOthers avoids interrupting existing audio, and the active
            # option restores other sessions when this server stops.
            self.audio_session.setCategory_withOptions_error_(
                "AVAudioSessionCategoryPlayback", 1, None
            )
            self.audio_session.setActive_withOptions_error_(True, 1, None)
            self.native_session_active = True
            return True
        except Exception as error:
            self.session_error = error
            self.audio_session = None
            self.native_session_active = False
            return False

    def _start_native_player(self):
        from objc_util import ObjCClass

        audio_player_class = ObjCClass("AVAudioPlayer")
        url_class = ObjCClass("NSURL")
        audio_url = url_class.fileURLWithPath_(self.audio_path)
        self.player = audio_player_class.alloc().initWithContentsOfURL_error_(
            audio_url, None
        )
        if self.player is None:
            raise RuntimeError("AVAudioPlayer could not open the generated audio file")

        self.player.setNumberOfLoops_(-1)
        self.player.prepareToPlay()
        if not self._activate_native_audio_session():
            raise RuntimeError(
                "AVAudioSession playback category could not be activated: {}".format(
                    self.session_error
                )
            )
        if not self.player.play():
            raise RuntimeError("AVAudioPlayer refused to start playback")
        self.player_backend = "AVAudioPlayer"

    def _start_pythonista_player(self):
        import sound

        self.player = sound.Player(self.audio_path)
        self.player.number_of_loops = -1
        # sound.Player construction can reset the shared session to an ambient
        # category, so activate playback only after the player exists.
        self._activate_native_audio_session()
        self.player.play()
        self.player_backend = "sound.Player"

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
            self._create_audio_file()
            try:
                self._start_pyto_background_task()
            except (ImportError, ModuleNotFoundError):
                self._check_host_background_audio_mode()
                try:
                    self._start_native_player()
                except Exception as error:
                    self.player_error = error
                    self.player = None
                    self._start_pythonista_player()
        except Exception as error:
            self.error = error
            self.player = None
            self.background_task = None
            self.player_backend = None
            return False

        return True

    def stop(self):
        if self.background_task is not None:
            background_task = self.background_task
            self.background_task = None
            self.player_backend = None
            background_task.stop()
        if self.player is not None:
            self.player.stop()
            self.player = None
            self.player_backend = None
        if self.native_session_active:
            self.audio_session.setActive_withOptions_error_(False, 1, None)
            self.native_session_active = False
            self.audio_session = None
