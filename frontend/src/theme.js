// PaperMind MiMo 风格主题系统
// 参考小米 MiMo: https://mimo.mi.com/

export const colors = {
  // 背景
  pageBg: '#f5f5f5',
  pageBgWarm: '#f6f3ec',
  cardBg: '#ffffff',

  // 文字
  textPrimary: '#1f2329',
  textSecondary: '#646a73',
  textTertiary: '#8f959e',

  // 强调色
  primary: '#249aff',
  primaryHover: '#1a8ced',
  accent: '#fb8147',
  accentHover: '#e56f39',

  // 边框/分隔
  border: '#f0f0f0',
  divider: '#dee0e3',

  // 状态
  success: '#34c759',
  error: '#ff3b30',
  warning: '#fc8800',
  info: '#249aff',

  // 特殊
  glassBg: 'rgba(255, 255, 255, 0.85)',
  shadow: '0 4px 20px rgba(0, 0, 0, 0.06)',
  shadowHover: '0 8px 30px rgba(0, 0, 0, 0.1)',
}

export const themeTokens = {
  colorPrimary: colors.primary,
  colorSuccess: colors.success,
  colorWarning: colors.warning,
  colorError: colors.error,
  colorInfo: colors.info,
  colorText: colors.textPrimary,
  colorTextSecondary: colors.textSecondary,
  colorTextTertiary: colors.textTertiary,
  colorBgLayout: colors.pageBg,
  colorBgContainer: colors.cardBg,
  colorBorder: colors.border,
  colorBorderSecondary: colors.divider,
  borderRadius: 12,
  borderRadiusLG: 16,
  borderRadiusSM: 8,
  boxShadow: colors.shadow,
  boxShadowSecondary: colors.shadowHover,
  fontFamily:
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans SC", sans-serif',
}

export const componentStyles = {
  // 卡片：大圆角、轻微阴影
  card: {
    borderRadius: 16,
    boxShadow: colors.shadow,
    background: colors.cardBg,
  },

  // 玻璃拟态 Header
  glassHeader: {
    background: colors.glassBg,
    backdropFilter: 'blur(12px)',
    WebkitBackdropFilter: 'blur(12px)',
    borderBottom: `1px solid ${colors.border}`,
  },

  // 侧边栏
  sider: {
    background: colors.cardBg,
    borderRight: `1px solid ${colors.border}`,
  },

  // 按钮：圆角胶囊
  buttonPrimary: {
    borderRadius: 24,
    boxShadow: '0 2px 8px rgba(36, 154, 255, 0.25)',
  },

  // 悬浮按钮
  fab: {
    position: 'fixed',
    right: 24,
    bottom: 24,
    zIndex: 1000,
    borderRadius: 28,
    boxShadow: colors.shadowHover,
  },
}

export default {
  colors,
  themeTokens,
  componentStyles,
}
