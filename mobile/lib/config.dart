/// Static configuration for the ops app.
///
/// The base URL defaults to the production ops host but is overridable two
/// ways: a build-time `--dart-define=OPS_BASE_URL=...` and, at runtime, the
/// value the owner enters on the login screen (persisted in secure storage).
/// Runtime wins over the compile-time default.
class Config {
  /// Compile-time default; the login screen can override and persist another.
  static const String defaultBaseUrl = String.fromEnvironment(
    'OPS_BASE_URL',
    defaultValue: 'https://ops.luminapp.org',
  );

  /// Network timeout for a single ops API call.
  static const Duration requestTimeout = Duration(seconds: 15);

  /// App display name shown in the app bar / lock screen.
  static const String appName = '360 CE Ops';
}
