"""CI build-time patches for Flet Android APK packaging:
1. Fixes flet_charts tooltip assertion bug (.nonNulls.toList()).
2. Fixes flet_geolocator nested foreground_notification_config parsing in Dart.
3. Adds get_wifi_ssids and update_notification MethodChannel handlers for
   persistent Navi-grade foreground notification updates and Wi-Fi SSID scanning.
"""

from __future__ import annotations

import io
import pathlib
import site
import tarfile
import urllib.request


def patch_site_packages() -> None:
    for pkg_root in site.getsitepackages():
        root = pathlib.Path(pkg_root)

        # 1. Patch flet-charts tooltip assertion bug
        fc = root / "flutter/flet_charts/lib/src/utils/line_chart.dart"
        if fc.exists():
            t = fc.read_text("utf-8")
            t = t.replace(".nonNulls\n          .toList()", ".toList()").replace(
                ".nonNulls.toList()", ".toList()"
            )
            fc.write_text(t, "utf-8")
            print("Successfully patched flet_charts:", fc)

        # 2. Patch flet_geolocator utils/geolocator.dart
        fgu = root / "flutter/flet_geolocator/lib/src/utils/geolocator.dart"
        if fgu.exists():
            t = fgu.read_text("utf-8")
            t = t.replace(
                "LocationSettings? parseLocationSettings(dynamic value) {",
                "LocationSettings? parseLocationSettings(dynamic value, {bool forSingleShot = false}) {",
            )
            old_block = """    return AndroidSettings(
      accuracy: accuracy,
      distanceFilter: distanceFilter,
      timeLimit: timeLimit,
      forceLocationManager: parseBool(value["force_location_manager"], false)!,
      intervalDuration: parseDuration(value["interval_duration"]),
      foregroundNotificationConfig:
          value["foreground_notification_text"] != null ||
                  value["foreground_notification_title"] != null
              ? ForegroundNotificationConfig(
                  notificationText: value["foreground_notification_text"] ?? "",
                  notificationTitle:
                      value["foreground_notification_title"] ?? "",
                  enableWakeLock: parseBool(
                      value["foreground_notification_enable_wake_lock"],
                      false)!,
                  enableWifiLock: parseBool(
                      value["foreground_notification_enable_wifi_lock"],
                      false)!,
                  setOngoing: parseBool(
                      value["foreground_notification_set_ongoing"], true)!,
                  color: parseColor(value["foreground_notification_color"]),
                  notificationChannelName:
                      value["foreground_notification_channel_name"] ??
                          'Background Location',
                )
              : null,
      useMSLAltitude: parseBool(value["use_msl_altitude"], false)!,
    );"""
            new_block = """    final fnc = value["foreground_notification_config"] is Map
        ? value["foreground_notification_config"] as Map
        : null;
    final notifTitle = fnc?["notification_title"] ?? value["foreground_notification_title"];
    final notifText = fnc?["notification_text"] ?? value["foreground_notification_text"];
    final setOngoing = parseBool(
        fnc?["notification_set_ongoing"] ?? value["foreground_notification_set_ongoing"],
        true)!;
    return AndroidSettings(
      accuracy: accuracy,
      distanceFilter: distanceFilter,
      timeLimit: timeLimit,
      forceLocationManager: parseBool(value["force_location_manager"], false)!,
      intervalDuration: parseDuration(value["interval_duration"]),
      foregroundNotificationConfig: (!forSingleShot && setOngoing && (notifText != null || notifTitle != null))
          ? ForegroundNotificationConfig(
              notificationText: notifText ?? "RIN-Live aktiv",
              notificationTitle: notifTitle ?? "RIN-Live",
              enableWakeLock: parseBool(
                  fnc?["notification_enable_wake_lock"] ?? value["foreground_notification_enable_wake_lock"],
                  true)!,
              enableWifiLock: parseBool(
                  fnc?["notification_enable_wifi_lock"] ?? value["foreground_notification_enable_wifi_lock"],
                  true)!,
              setOngoing: setOngoing,
              color: parseColor(fnc?["notification_color"] ?? value["foreground_notification_color"]),
              notificationChannelName: (fnc?["notification_channel_name"] ??
                      value["foreground_notification_channel_name"]) ??
                  'RIN-Live Aufzeichnung',
            )
          : null,
      useMSLAltitude: parseBool(value["use_msl_altitude"], false)!,
    );"""
            if old_block in t:
                t = t.replace(old_block, new_block)
                fgu.write_text(t, "utf-8")
                print("Successfully patched flet_geolocator utils:", fgu)

        # 3. Patch flet_geolocator geolocator.dart
        fgd = root / "flutter/flet_geolocator/lib/src/geolocator.dart"
        if fgd.exists():
            t = fgd.read_text("utf-8")
            if "package:flutter/services.dart" not in t:
                t = "import 'package:flutter/services.dart';\n" + t
            t = t.replace(
                """        var locationSettings = args["location_settings"] != null
            ? parseLocationSettings(args["location_settings"])
            : this.locationSettings;""",
                """        var locationSettings = args["location_settings"] != null
            ? parseLocationSettings(args["location_settings"], forSingleShot: true)
            : parseLocationSettings(control.get("configuration"), forSingleShot: true);""",
            )
            old_switch_end = """      case "open_location_settings":
        return await Geolocator.openLocationSettings();
      default:"""
            new_switch_end = """      case "open_location_settings":
        return await Geolocator.openLocationSettings();
      case "get_wifi_ssids":
        if (defaultTargetPlatform == TargetPlatform.android && !kIsWeb) {
          const ch = MethodChannel('flutter.baseflow.com/geolocator_android');
          return await ch.invokeMethod('getWifiSsids');
        }
        return <String>[];
      case "update_notification":
        if (defaultTargetPlatform == TargetPlatform.android && !kIsWeb) {
          const ch = MethodChannel('flutter.baseflow.com/geolocator_android');
          return await ch.invokeMethod('updateNotification', args);
        }
        return false;
      default:"""
            if old_switch_end in t:
                t = t.replace(old_switch_end, new_switch_end)
                fgd.write_text(t, "utf-8")
                print("Successfully patched flet_geolocator service:", fgd)


def patch_geolocator_android() -> None:
    ga_dir = pathlib.Path("/tmp/geolocator_android")
    ga_dir.mkdir(parents=True, exist_ok=True)
    url = "https://pub.dev/api/archives/geolocator_android-5.2.1.tar.gz"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(ga_dir)

    # Patch BackgroundNotification.java
    bn_file = ga_dir / "android/src/main/java/com/baseflow/geolocator/location/BackgroundNotification.java"
    bn_text = bn_file.read_text("utf-8")
    bn_text = bn_text.replace("NotificationManager.IMPORTANCE_NONE", "NotificationManager.IMPORTANCE_LOW")
    bn_text = bn_text.replace(
        'getDrawableId("ic_launcher.png", "mipmap");',
        'iconId = getDrawableId("ic_launcher", "mipmap");\n      if (iconId == 0) { iconId = android.R.drawable.ic_menu_mylocation; }',
    )
    bn_text = bn_text.replace(
        ".setOngoing(options.isSetOngoing())",
        ".setOngoing(options.isSetOngoing())\n            .setOnlyAlertOnce(true)\n            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)\n            .setCategory(NotificationCompat.CATEGORY_NAVIGATION)\n            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)",
    )
    bn_file.write_text(bn_text, "utf-8")
    print("Successfully patched BackgroundNotification.java")

    # Patch GeolocatorLocationService.java
    gls_file = ga_dir / "android/src/main/java/com/baseflow/geolocator/GeolocatorLocationService.java"
    gls_text = gls_file.read_text("utf-8")
    gls_text = gls_text.replace(
        "public class GeolocatorLocationService extends Service {",
        "public class GeolocatorLocationService extends Service {\n  public static GeolocatorLocationService activeInstance;",
    )
    gls_text = gls_text.replace(
        "super.onCreate();",
        "super.onCreate();\n    activeInstance = this;",
    )
    gls_text = gls_text.replace(
        "super.onDestroy();",
        "if (activeInstance == this) { activeInstance = null; }\n    super.onDestroy();",
    )
    gls_file.write_text(gls_text, "utf-8")
    print("Successfully patched GeolocatorLocationService.java")

    # Patch MethodCallHandlerImpl.java
    mch_file = ga_dir / "android/src/main/java/com/baseflow/geolocator/MethodCallHandlerImpl.java"
    mch_text = mch_file.read_text("utf-8")
    imports_add = """import android.net.wifi.ScanResult;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import com.baseflow.geolocator.location.ForegroundNotificationOptions;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
"""
    mch_text = mch_text.replace("import android.content.Context;", "import android.content.Context;\n" + imports_add)
    mch_text = mch_text.replace(
        "default:\n        result.notImplemented();\n        break;",
        """case "getWifiSsids":
        onGetWifiSsids(result);
        break;
      case "updateNotification":
        onUpdateNotification(call, result);
        break;
      default:
        result.notImplemented();
        break;""",
    )
    helper_methods = """
  private long lastWifiScanMs = 0;

  @SuppressWarnings("deprecation")
  private void onGetWifiSsids(MethodChannel.Result result) {
    Set<String> ssids = new LinkedHashSet<>();
    try {
      if (context != null) {
        WifiManager wm = (WifiManager) context.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
          long now = System.currentTimeMillis();
          if (now - lastWifiScanMs > 15000) {
            lastWifiScanMs = now;
            try { wm.startScan(); } catch (Exception ignored) {}
          }
          try {
            WifiInfo info = wm.getConnectionInfo();
            if (info != null && info.getSSID() != null) {
              String connectedSsid = info.getSSID().replace("\\"", "").trim();
              if (!connectedSsid.isEmpty() && !"<unknown ssid>".equalsIgnoreCase(connectedSsid)) {
                ssids.add(connectedSsid);
              }
            }
          } catch (Exception ignored) {}
          try {
            List<ScanResult> scans = wm.getScanResults();
            if (scans != null) {
              for (ScanResult sr : scans) {
                if (sr != null && sr.SSID != null) {
                  String s = sr.SSID.replace("\\"", "").trim();
                  if (!s.isEmpty()) {
                    ssids.add(s);
                  }
                }
              }
            }
          } catch (Exception ignored) {}
        }
      }
    } catch (Exception ignored) {}
    result.success(new ArrayList<>(ssids));
  }

  @SuppressWarnings("unchecked")
  private void onUpdateNotification(MethodCall call, MethodChannel.Result result) {
    try {
      Map<String, Object> map = (Map<String, Object>) call.arguments;
      GeolocatorLocationService svc = GeolocatorLocationService.activeInstance;
      if (svc != null && map != null) {
        Boolean ongoing = (Boolean) map.get("setOngoing");
        if (ongoing != null && !ongoing) {
          svc.disableBackgroundMode();
        } else {
          ForegroundNotificationOptions opts = ForegroundNotificationOptions.parseArguments(map);
          if (opts != null) {
            svc.changeNotificationOptions(opts);
            svc.enableBackgroundMode(opts);
          }
        }
        result.success(true);
        return;
      }
    } catch (Exception ignored) {}
    result.success(false);
  }
}
"""
    last_brace = mch_text.rfind("}")
    mch_text = mch_text[:last_brace] + helper_methods
    mch_file.write_text(mch_text, "utf-8")
    print("Successfully patched MethodCallHandlerImpl.java")

    pyproj = pathlib.Path("pyproject.toml")
    pt = pyproj.read_text("utf-8")
    if "geolocator_android" not in pt:
        pt = pt.replace(
            'jni = "1.1.0"',
            'jni = "1.1.0"\ngeolocator_android = { path = "/tmp/geolocator_android" }',
        )
        pyproj.write_text(pt, "utf-8")
        print("Registered patched geolocator_android in pyproject.toml")


if __name__ == "__main__":
    patch_site_packages()
    patch_geolocator_android()

