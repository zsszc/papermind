import { useEffect, useState } from 'react'
import { Modal, Form, Input, Button, message, Spin } from 'antd'
import { getSettings, updateSettings } from '../api'
import { colors, componentStyles } from '../theme'

function SettingsModal({ open, onClose }) {
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [maskedKey, setMaskedKey] = useState('')

  useEffect(() => {
    if (!open) return
    setLoading(true)
    getSettings()
      .then((res) => {
        const data = res.data || {}
        setMaskedKey(data.llm_api_key || '')
        form.setFieldsValue({
          llm_api_key: data.llm_api_key || '',
          llm_model: data.llm_model || '',
          llm_base_url: data.llm_base_url || '',
        })
      })
      .catch(() => message.error('加载设置失败'))
      .finally(() => setLoading(false))
  }, [open, form])

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      await updateSettings(values)
      message.success('设置已保存，重启应用后生效')
      onClose()
    } catch (err) {
      if (err.response?.data?.detail) {
        message.error(err.response.data.detail)
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title="设置"
      open={open}
      onCancel={onClose}
      footer={[
        <Button key="cancel" onClick={onClose}>
          取消
        </Button>,
        <Button
          key="save"
          type="primary"
          loading={saving}
          onClick={handleSave}
          style={componentStyles.buttonPrimary}
        >
          保存
        </Button>,
      ]}
      bodyStyle={{ paddingTop: 12 }}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin tip="加载设置中..." />
        </div>
      ) : (
        <Form form={form} layout="vertical" autoComplete="off">
          <Form.Item
            name="llm_api_key"
            label="Kimi API Key"
            extra={maskedKey?.includes('*') ? '当前显示为脱敏值，如需修改请直接输入新 key' : ''}
            rules={[{ required: true, message: '请输入 API Key' }]}
          >
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Form.Item name="llm_model" label="模型">
            <Input placeholder="例如 kimi-k2.6" />
          </Form.Item>
          <Form.Item name="llm_base_url" label="Base URL">
            <Input placeholder="https://api.moonshot.cn/v1" />
          </Form.Item>
          <div style={{ color: colors.textSecondary, fontSize: 12, marginTop: 8 }}>
            提示：修改 API Key 后需要完全退出并重新打开应用才会生效。
          </div>
        </Form>
      )}
    </Modal>
  )
}

export default SettingsModal
