import 'package:flutter/material.dart';

/// Material-3 theme for the ops app. Dark-first (this is a night-ops control
/// panel) with a light variant that follows the system setting. Single accent
/// seed so light/dark stay one visual system.
class OpsTheme {
  static const Color _seed = Color(0xFF3B82F6); // blue-500

  static ThemeData dark() => _build(Brightness.dark);
  static ThemeData light() => _build(Brightness.light);

  static ThemeData _build(Brightness brightness) {
    final scheme = ColorScheme.fromSeed(
      seedColor: _seed,
      brightness: brightness,
    );
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      scaffoldBackgroundColor: scheme.surface,
      appBarTheme: AppBarTheme(
        backgroundColor: scheme.surface,
        foregroundColor: scheme.onSurface,
        centerTitle: false,
        elevation: 0,
      ),
      // Card styling is applied per-widget (see widgets/cards.dart) rather than
      // via ThemeData.cardTheme — the cardTheme parameter's type name drifts
      // across Flutter stable releases (CardTheme vs CardThemeData), so we
      // avoid it to keep the build green on the floating `stable` channel.
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: scheme.surface,
        indicatorColor: scheme.primary.withOpacity(0.16),
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
      ),
      snackBarTheme: const SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
      ),
    );
  }

  /// Semantic status colors used by pills/badges across screens.
  static Color statusColor(ColorScheme scheme, String status) {
    switch (status.toLowerCase()) {
      case 'ok':
      case 'healthy':
      case 'running':
      case 'live':
      case 'up':
        return const Color(0xFF16A34A); // green-600
      case 'warn':
      case 'warning':
      case 'paper':
      case 'degraded':
        return const Color(0xFFD97706); // amber-600
      case 'error':
      case 'down':
      case 'tripped':
      case 'unreachable':
      case 'off':
        return const Color(0xFFDC2626); // red-600
      default:
        return scheme.outline;
    }
  }
}
