import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import {
  MinusOutlined,
  ExpandAltOutlined,
  ColumnWidthOutlined,
  FilePdfOutlined,
  RobotOutlined,
  EditOutlined,
} from '@ant-design/icons'
import { Tooltip } from 'antd'
import { colors } from '../theme'

const DIVIDER_WIDTH = 6
const MINIMIZED_WIDTH = 56
const MIN_ACTIVE_WIDTH = 140

const ICON_MAP = {
  pdf: FilePdfOutlined,
  ai: RobotOutlined,
  note: EditOutlined,
}

function clamp(num, min, max) {
  return Math.max(min, Math.min(max, num))
}

export default function ResizablePanels({
  storageKey,
  panels,
  style,
  className,
}) {
  const containerRef = useRef(null)
  const [tick, setTick] = useState(0)
  const [dragging, setDragging] = useState(false)

  const defaultRatios = useMemo(
    () => panels.map((p) => p.defaultRatio || 1),
    [panels]
  )

  const [ratios, setRatios] = useState(() => defaultRatios)
  const [minimized, setMinimized] = useState(() => new Set())

  // 从 localStorage 恢复布局
  useEffect(() => {
    if (!storageKey) return
    try {
      const raw = localStorage.getItem(`ResizablePanels:${storageKey}`)
      if (raw) {
        const saved = JSON.parse(raw)
        if (saved.ratios && saved.ratios.length === panels.length) {
          setRatios(saved.ratios)
        }
        if (saved.minimized) {
          setMinimized(new Set(saved.minimized))
        }
      }
    } catch (e) {
      // ignore
    }
  }, [storageKey, panels.length])

  // 保存布局
  useEffect(() => {
    if (!storageKey) return
    localStorage.setItem(
      `ResizablePanels:${storageKey}`,
      JSON.stringify({ ratios, minimized: Array.from(minimized) })
    )
  }, [ratios, minimized, storageKey])

  // 监听容器尺寸变化
  useEffect(() => {
    if (!containerRef.current) return
    if (typeof ResizeObserver === 'undefined') {
      const onResize = () => setTick((t) => t + 1)
      window.addEventListener('resize', onResize)
      return () => window.removeEventListener('resize', onResize)
    }
    const ro = new ResizeObserver(() => setTick((t) => t + 1))
    ro.observe(containerRef.current)
    return () => ro.disconnect()
  }, [])

  const containerWidth = containerRef.current?.clientWidth || 0

  const widths = useMemo(() => {
    const n = panels.length
    const dividerTotal = (n - 1) * DIVIDER_WIDTH
    const minimizedTotal = minimized.size * MINIMIZED_WIDTH
    const available = Math.max(0, containerWidth - dividerTotal - minimizedTotal)
    const activeRatios = ratios.map((r, i) => (minimized.has(i) ? 0 : r))
    const total = activeRatios.reduce((a, b) => a + b, 0) || 1
    return ratios.map((r, i) =>
      minimized.has(i) ? MINIMIZED_WIDTH : (r / total) * available
    )
  }, [ratios, minimized, containerWidth, panels.length])

  const handleMouseDown = useCallback(
    (e, index) => {
      if (minimized.has(index) || minimized.has(index + 1)) return
      e.preventDefault()
      setDragging(true)
      const startX = e.clientX
      const startWidths = [...widths]
      const activeIndices = ratios
        .map((_, i) => i)
        .filter((i) => !minimized.has(i))
      const activeTotalRatio = activeIndices.reduce(
        (sum, i) => sum + ratios[i],
        0
      )
      const available = startWidths[index] + startWidths[index + 1]

      const onMove = (ev) => {
        const delta = ev.clientX - startX
        const newLeft = clamp(startWidths[index] + delta, MIN_ACTIVE_WIDTH, available - MIN_ACTIVE_WIDTH)
        const newRight = available - newLeft

        const nextRatios = [...ratios]
        const scale = activeTotalRatio / available
        nextRatios[index] = newLeft * scale
        nextRatios[index + 1] = newRight * scale
        setRatios(nextRatios)
      }

      const onUp = () => {
        setDragging(false)
        document.removeEventListener('mousemove', onMove)
        document.removeEventListener('mouseup', onUp)
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
      }

      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      document.addEventListener('mousemove', onMove)
      document.addEventListener('mouseup', onUp)
    },
    [minimized, ratios, widths]
  )

  const toggleMinimize = useCallback(
    (index) => {
      setMinimized((prev) => {
        const next = new Set(prev)
        if (next.has(index)) {
          next.delete(index)
        } else {
          // 至少保留一个非最小化面板
          const activeCount = panels.length - next.size
          if (activeCount <= 1) return prev
          next.add(index)
        }
        return next
      })
    },
    [panels.length]
  )

  const reset = useCallback(() => {
    setRatios(defaultRatios)
    setMinimized(new Set())
  }, [defaultRatios])

  return (
    <div
      ref={containerRef}
      className={className}
      style={{
        display: 'flex',
        flexDirection: 'row',
        width: '100%',
        height: '100%',
        overflow: 'hidden',
        ...style,
      }}
    >
      {panels.map((panel, index) => {
        const isMinimized = minimized.has(index)
        const Icon = panel.icon ? ICON_MAP[panel.icon] || panel.icon : null
        return (
          <div key={panel.key} style={{ display: 'flex', flexDirection: 'row', height: '100%' }}>
            <div
              style={{
                width: widths[index],
                minWidth: isMinimized ? MINIMIZED_WIDTH : MIN_ACTIVE_WIDTH,
                height: '100%',
                display: 'flex',
                flexDirection: 'column',
                background: '#fff',
                borderRadius: 16,
                border: `1px solid ${colors.border}`,
                overflow: 'hidden',
                boxShadow: '0 2px 8px rgba(0,0,0,0.04)',
              }}
            >
              {/* 标题栏 */}
              <div
                style={{
                  height: 46,
                  flexShrink: 0,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: isMinimized ? '0 8px' : '0 12px 0 16px',
                  borderBottom: `1px solid ${colors.border}`,
                  background: colors.pageBg,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    overflow: 'hidden',
                    flex: 1,
                  }}
                >
                  {Icon && (
                    <Icon
                      style={{
                        color: colors.primary,
                        fontSize: 16,
                        flexShrink: 0,
                      }}
                    />
                  )}
                  {!isMinimized && (
                    <span
                      style={{
                        fontWeight: 600,
                        color: colors.textPrimary,
                        whiteSpace: 'nowrap',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                      }}
                    >
                      {panel.title}
                    </span>
                  )}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>
                  {!isMinimized && (
                    <Tooltip title="恢复默认布局">
                      <ColumnWidthOutlined
                        onClick={reset}
                        style={{
                          cursor: 'pointer',
                          fontSize: 14,
                          color: colors.textTertiary,
                          padding: '4px 6px',
                          borderRadius: 6,
                        }}
                        onMouseEnter={(e) =>
                          (e.currentTarget.style.background = '#f0f0f0')
                        }
                        onMouseLeave={(e) =>
                          (e.currentTarget.style.background = 'transparent')
                        }
                      />
                    </Tooltip>
                  )}
                  <Tooltip title={isMinimized ? '展开' : '最小化'}>
                    {isMinimized ? (
                      <ExpandAltOutlined
                        onClick={() => toggleMinimize(index)}
                        style={{
                          cursor: 'pointer',
                          fontSize: 14,
                          color: colors.textTertiary,
                          padding: '4px 6px',
                          borderRadius: 6,
                        }}
                        onMouseEnter={(e) =>
                          (e.currentTarget.style.background = '#f0f0f0')
                        }
                        onMouseLeave={(e) =>
                          (e.currentTarget.style.background = 'transparent')
                        }
                      />
                    ) : (
                      <MinusOutlined
                        onClick={() => toggleMinimize(index)}
                        style={{
                          cursor: 'pointer',
                          fontSize: 14,
                          color: colors.textTertiary,
                          padding: '4px 6px',
                          borderRadius: 6,
                        }}
                        onMouseEnter={(e) =>
                          (e.currentTarget.style.background = '#f0f0f0')
                        }
                        onMouseLeave={(e) =>
                          (e.currentTarget.style.background = 'transparent')
                        }
                      />
                    )}
                  </Tooltip>
                </div>
              </div>

              {/* 内容区 */}
              <div
                style={{
                  flex: 1,
                  overflow: 'hidden',
                  display: isMinimized ? 'none' : 'block',
                }}
              >
                {panel.content}
              </div>
            </div>

            {index < panels.length - 1 && (
              <div
                onMouseDown={(e) => handleMouseDown(e, index)}
                style={{
                  width: DIVIDER_WIDTH,
                  flexShrink: 0,
                  cursor: minimized.has(index) || minimized.has(index + 1) ? 'not-allowed' : 'col-resize',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: 'transparent',
                }}
              >
                <div
                  style={{
                    width: 2,
                    height: 36,
                    borderRadius: 2,
                    background: dragging ? colors.primary : colors.border,
                    transition: 'background 0.2s',
                  }}
                />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
