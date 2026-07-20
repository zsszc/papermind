import { useState } from 'react'
import { Card, Button, Space, Select, Typography, message, Row, Col } from 'antd'
import {
  FileExcelOutlined,
  FileTextOutlined,
  BookOutlined,
  CloudDownloadOutlined,
  SafetyOutlined,
} from '@ant-design/icons'
import {
  exportPapersCSV,
  exportPapersExcel,
  exportPapersBib,
  exportBackup,
  triggerAutoBackup,
} from '../api'
import { colors, componentStyles } from '../theme'

const { Title, Text } = Typography
const { Option } = Select

function downloadBlob(response, fallbackFilename) {
  const blob = new Blob([response.data])
  const url = window.URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url

  let filename = fallbackFilename
  try {
    const disposition = response.headers?.['content-disposition'] || ''
    const match = disposition.match(/filename="?([^"]+)"?/)
    if (match) filename = match[1]
  } catch {
    // ignore
  }

  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.URL.revokeObjectURL(url)
}

function DataExport() {
  const [citationFormat, setCitationFormat] = useState('GB/T 7714')
  const [loading, setLoading] = useState({})

  const setBusy = (key, busy) => setLoading((prev) => ({ ...prev, [key]: busy }))

  const handleExportCSV = async () => {
    setBusy('csv', true)
    try {
      const res = await exportPapersCSV()
      downloadBlob(res, 'papermind_papers.csv')
      message.success('CSV 导出成功')
    } catch {
      message.error('CSV 导出失败')
    } finally {
      setBusy('csv', false)
    }
  }

  const handleExportExcel = async () => {
    setBusy('excel', true)
    try {
      const res = await exportPapersExcel()
      downloadBlob(res, 'papermind_papers.xlsx')
      message.success('Excel 导出成功')
    } catch {
      message.error('Excel 导出失败')
    } finally {
      setBusy('excel', false)
    }
  }

  const handleExportBib = async () => {
    setBusy('bib', true)
    try {
      const res = await exportPapersBib(citationFormat)
      downloadBlob(res, 'papermind_citations.txt')
      message.success('引用列表导出成功')
    } catch {
      message.error('引用列表导出失败')
    } finally {
      setBusy('bib', false)
    }
  }

  const handleBackup = async () => {
    setBusy('backup', true)
    try {
      const res = await exportBackup()
      downloadBlob(res, 'papermind_backup.zip')
      message.success('全量备份导出成功')
    } catch {
      message.error('备份导出失败')
    } finally {
      setBusy('backup', false)
    }
  }

  const handleAutoBackup = async () => {
    setBusy('autoBackup', true)
    try {
      const res = await triggerAutoBackup()
      message.success(`自动备份已触发：${res.data.path}`)
    } catch {
      message.error('自动备份失败')
    } finally {
      setBusy('autoBackup', false)
    }
  }

  return (
    <div>
      <Title level={5} style={{ color: colors.textPrimary, marginBottom: 16 }}>
        数据导出与备份
      </Title>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <Card
            title="文献元数据导出"
            style={{ ...componentStyles.card, height: '100%' }}
            bodyStyle={{ padding: '20px 24px' }}
          >
            <Text style={{ color: colors.textSecondary, display: 'block', marginBottom: 16 }}>
              导出文献列表的标题、作者、年份、期刊、DOI 等元数据。
            </Text>
            <Space wrap>
              <Button
                icon={<FileTextOutlined />}
                onClick={handleExportCSV}
                loading={loading.csv}
                style={{ borderRadius: 20 }}
              >
                导出 CSV
              </Button>
              <Button
                type="primary"
                icon={<FileExcelOutlined />}
                onClick={handleExportExcel}
                loading={loading.excel}
                style={componentStyles.buttonPrimary}
              >
                导出 Excel
              </Button>
            </Space>
          </Card>
        </Col>

        <Col xs={24} md={12}>
          <Card
            title="引用列表导出"
            style={{ ...componentStyles.card, height: '100%' }}
            bodyStyle={{ padding: '20px 24px' }}
          >
            <Text style={{ color: colors.textSecondary, display: 'block', marginBottom: 16 }}>
              按指定格式导出所有文献的引用条目。
            </Text>
            <Space wrap>
              <Select
                value={citationFormat}
                onChange={setCitationFormat}
                style={{ width: 160 }}
              >
                <Option value="GB/T 7714">GB/T 7714</Option>
                <Option value="APA">APA</Option>
                <Option value="MLA">MLA</Option>
              </Select>
              <Button
                icon={<BookOutlined />}
                onClick={handleExportBib}
                loading={loading.bib}
                style={{ borderRadius: 20 }}
              >
                导出引用列表
              </Button>
            </Space>
          </Card>
        </Col>

        <Col xs={24}>
          <Card
            title="全量备份"
            style={{ ...componentStyles.card }}
            bodyStyle={{ padding: '20px 24px' }}
          >
            <Text style={{ color: colors.textSecondary, display: 'block', marginBottom: 16 }}>
              打包数据库、PDF、笔记、大论文、向量库等全部本地数据，便于迁移或恢复。
            </Text>
            <Space wrap>
              <Button
                type="primary"
                icon={<CloudDownloadOutlined />}
                onClick={handleBackup}
                loading={loading.backup}
                style={componentStyles.buttonPrimary}
              >
                导出全量备份 (zip)
              </Button>
              <Button
                icon={<SafetyOutlined />}
                onClick={handleAutoBackup}
                loading={loading.autoBackup}
                style={{ borderRadius: 20 }}
              >
                立即执行自动备份
              </Button>
            </Space>
          </Card>
        </Col>
      </Row>
    </div>
  )
}

export default DataExport
