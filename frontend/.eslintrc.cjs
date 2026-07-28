// ESLint 配置：基于 Vite React 模板，配合 package.json 中已装的插件
module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:react/recommended',
    'plugin:react/jsx-runtime',
    'plugin:react-hooks/recommended',
  ],
  // public/ 下为第三方压缩产物（pdf.worker.min.js），不参与 lint
  ignorePatterns: ['dist', 'node_modules', 'public', '.eslintrc.cjs'],
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  settings: { react: { version: 'detect' } },
  plugins: ['react-refresh'],
  rules: {
    // 项目未使用 prop-types，关闭以避免大量噪音
    'react/prop-types': 'off',
    // 现有代码存在大量有意省略依赖的 hooks，先关闭，后续再逐步收敛
    'react-hooks/exhaustive-deps': 'off',
    // 存量代码有若干未使用变量（涉及多个组件文件），先关闭，待统一清理后再开启
    'no-unused-vars': 'off',
    'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
  },
}
