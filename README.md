# What

A simple HTTP/SOCKS proxy designed to run on Pythonista on iOS, letting you fake-tether your devices to a phone. 

# Installation

- Install Pythonista from the [App Store](https://apps.apple.com/us/app/pythonista-3/id1085978097). It's a paid app, but it's worth every penny if you are a power user.
- Download the code from [GitHub](https://github.com/nneonneo/iOS-SOCKS-Server/archive/master.zip).
- Open the Files app, navigate to Downloads, and tap on the zip file to uncompress it.
- Move the resulting `iOS-SOCKS-Server` folder to the Pythonista iCloud directory
- Open Pythonista, navigate to iCloud, `iOS-SOCKS-Server` and open the `socks5.py` script.
- Optionally, you can tap on the wrench and select `Shortcuts...` to add the script to your home screen. 

# Running

- Connect your devices to the same WiFi network as your phone. If there's no suitable network, you can create a computer-to-computer (ad-hoc) network using your laptop and connect to it with your phone.
- Open the home screen shortcut (if you made one), or open the `socks5.py` script in Pythonista and hit Run. 
- By default, the script loops a generated silent audio file while it runs. This
  can let Pythonista continue serving after you switch apps or lock the screen.
  Set `KEEP_ALIVE_WITH_AUDIO = False` in `socks5.py` to disable it. iOS can still
  suspend or terminate Pythonista, so this is a best-effort workaround rather
  than a guarantee.
- Set `BACKGROUND_AUDIO_TEST_TONE = True` to play a quiet 440 Hz sine wave. This
  makes it easy to confirm that audio continues after locking the screen or
  switching apps. Set it back to `False` for normal silent operation.
- On iOS, the script also requests the native `AVAudioSessionCategoryPlayback`
  category and uses `AVAudioPlayer` directly so Pythonista does not own the
  player lifecycle. If the status display says `Pythonista player only` or
  `sound.Player`, the native APIs were unavailable and background playback is
  less likely to work.
- The playback category should ignore the iPhone Ring/Silent switch. If native
  playback still pauses whenever Pythonista leaves the foreground, the installed
  Pythonista build does not permit script-started background audio; a script
  cannot add the missing iOS app background-mode entitlement.
- Pyto's implementation works because its `Info.plist` declares `audio` under
  `UIBackgroundModes`. The script checks Pythonista's bundle for that declaration
  and prints a warning when it is absent. Neither an internal Python loop nor
  `UIApplication.beginBackgroundTask` can replace it for indefinite operation.
- When running in Pyto, the script uses Pyto's supported
  `background.BackgroundTask` API directly. This backend also restarts audio
  after an iOS audio-session interruption. The status display identifies it as
  `Pyto BackgroundTask`.
- Server shutdown bypasses Pyto's thread-interrupting `BackgroundTask.stop()`
  wrapper and stops its native task directly. Pyto's Stop button (`SystemExit`)
  and keyboard interrupts both trigger cleanup: listening sockets, active TCP
  tunnels, and UDP relays close, and background audio stops before waiting for
  coroutine cancellation. The remaining tasks get up to five seconds to finish.
  WPAD requests have a one-second socket timeout, and WPAD shutdown uses bounded
  thread waits. Cleanup also runs if startup fails, and a failed cleanup action
  does not prevent the other actions from running.
- By default, the server watches the Wi-Fi or hotspot bridge interface and IPv4 address present
  at startup as `Subaru_5G`. If that connection disappears or changes for six
  seconds, the proxy, WPAD server, and background audio are stopped. Start the
  script while connected to `Subaru_5G`; Pyto cannot reliably read the actual
  SSID without an iOS entitlement. This behavior is configured by the
  `EXIT_ON_WIFI_DISCONNECT`, `WIFI_NETWORK_NAME`, `WIFI_CHECK_INTERVAL`, and
  `WIFI_DISCONNECT_CHECKS` constants in `socks5.py`.
  Wi-Fi checks run on a synchronous monitor thread so they continue while Pyto
  is in the background. Shutdown explicitly closes the SOCKS, HTTP, and WPAD
  listening sockets before ending the background task. The monitor prints its
  selected interface at startup and reports each failed disconnect check.
- Point your devices at the PAC URL (also called script URL, script address, etc.), or configure them to use the SOCKS proxy listed.
    - For iOS devices: open Settings, tap on Wi-Fi, tap on the (i) icon next to the network, scroll down to HTTP Proxy, tap on Configure Proxy, select Automatic, and enter the PAC URL as displayed in Pythonista in the URL field (the URL will look like http://123.123.123.123:8080/wpad.dat).
    - For macOS: open System Preferences -> Network, click on Wi-Fi, hit Advanced..., and under Proxies check SOCKS Proxy and set the host:port to the SOCKS Address as displayed in Pythonista (this will be of the form 123.123.123.123:9876).
        - If you are using an ad-hoc Wi-Fi network (i.e. Wi-Fi menu -> Create Network), you will need to do some extra setup here. Under the TCP/IP tab, copy the existing 169.254.y.z IPv4 address, then switch Configure IPv4 to Manually, enter the 169.254.y.z IP address in both IPv4 Address and Router, and enter 255.255.0.0 as Subnet Mask. Under the DNS tab, add 169.254.y.z to the DNS Servers list.
        - Make sure you set proxy settings in any other application that is not using the system proxy settings.
    - For Windows or Linux, please follow the appropriate instructions for configuring a proxy on your system. It is recommended that you use the PAC URL if possible (also called a setup script or automatic configuration script).
        - On Windows, you may consider using the [SSTap](https://sourceforge.net/projects/sstap/) project to force all connections to go through the proxy. Disclaimer: this project does not have any affiliation with SSTap and cannot provide support for any issues that arise from its use.
    - For Android: open Settings, Wi-Fi, select your network, expand the Advanced Settings, change the proxy setting to Manual, and enter the host and port for the *HTTP proxy*. Note that SOCKS proxy support on Android is limited, even when using the PAC URL, so the HTTP proxy is recommended.
        - Many applications on Android do not respect proxy settings, unfortunately, and in those cases you will have to configure the apps manually or use an app like Proxifier to force apps to use the proxy.

# Why

Recently, while travelling, I found out that Google Fi doesn't support tethering on iOS (I guess it's a feature they want to keep Android-exclusive or something?). Since my phone has a nice, fast, unblocked connection, I wanted to let my computer access it too.

I previously wrote [Socks5-iOS](https://github.com/nneonneo/socks5-ios) for doing exactly this, but it turned out to be quite cumbersome to deploy and modify. Plus, the app expires frequently (if you don't have an iOS developer account), which makes it annoying if you need it in a pinch. Enter Pythonista - an App Store app which puts a complete Python interpreter on iOS.

This script can be used to implement a functional alternative to tethering, which I refer to fake-tethering. Fake-tethering has some substantial advantages over standard iOS tethering. It works even when carriers ban tethering, and it bypasses limits set on tethering speed since all connections originate from the phone.

While it's easiest to use this with websites, it's actually possible to tunnel any TCP connection over a SOCKS proxy. For example, here's how you would proxy an SSH connection:

`ssh -o ProxyCommand='nc -X 5 -x <IP>:9876 %h %p' user@host`

# Troubleshooting

## Clients connect but cannot access the internet

While the failure is happening, run `diagnose_network.py` in Pyto before
rebooting. It compares HTTPS using system DNS and the default route with direct
TCP and public DNS probes, both unbound and bound to cellular/VPN addresses.
Individual probes can fail because a destination is blocked; compare the
results rather than treating a single failure as proof the network is down.

- If default-route HTTPS passes but TCP bound to the interface shown by the
  proxy fails, try `USE_SYSTEM_DEFAULT_ROUTE = True` in `socks5.py`, then rerun
  the proxy. This disables automatic source binding. The default route may use
  Wi-Fi rather than cellular, so this option is suitable only when that route
  provides the internet access you need.
- If TCP passes but public DNS probes fail, the proxy's DNS path may be blocked.
  Set `CUSTOM_RESOLVERS` to DNS server IPs reachable through your cellular/VPN
  connection, then rerun. The proxy uses its own public DNS servers rather than
  Safari's system DNS; Safari working does not establish that this DNS path works.
- If a VPN is active, reconnect it and rerun the proxy. To test cellular without
  a VPN, disconnect the VPN and set `USE_PHONE_VPN = False`. Changing this flag
  alone does not disable iOS VPN routing or an always-on VPN policy.
- Check whether the script printed a Wi-Fi disconnect shutdown message. Its
  monitor deliberately stops the proxy when the startup Wi-Fi/hotspot address
  disappears or changes. Reconnect and rerun the script; check the displayed PAC
  URL if the phone's Wi-Fi address changed.

The proxy refreshes automatically selected source addresses before new TCP
connections and UDP destination lookups. DNS source binding is refreshed at the
same time. If the selected interface disappears entirely, requests fail with an
explicit error until it returns; rerun the script if iOS assigns a different
interface name. Existing TCP connections and UDP associations still need to be
reopened after a network change. HTTP client sockets now close on error paths
as well as successful requests.

## Doesn't work with an ad-hoc network on macOS

macOS appears to incorrectly assess the Internet as unreachable with an ad-hoc network, even if a proxy is configured. A workaround for this, tested on macOS 10.14, is described under [issue #1](https://github.com/nneonneo/iOS-SOCKS-Server/issues/1#issuecomment-583989079).
