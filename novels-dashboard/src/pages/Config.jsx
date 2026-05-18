import React, { useEffect, useState } from 'react'
import {
  Form, Input, InputNumber, Switch, Select, Button, Space,
  Typography, Card, Tabs, Toast, Skeleton,
} from '@douyinfe/semi-ui'
import { IconSave, IconCopy } from '@douyinfe/semi-icons'
import { novelApi } from '../api/novelApi'

const { Title } = Typography
const { TabPane } = Tabs

const PROJECT_OPTIONS = [
  { value: 'novels1', label: 'novels1' },
  { value: 'novels2', label: 'novels2' },
  { value: 'novels3', label: 'novels3' },
  { value: 'novels4', label: 'novels4' },
  { value: 'novels5', label: 'novels5' },
  { value: 'novels6', label: 'novels6' },
  { value: 'novels7', label: 'novels7' },
]

function Config() {
  const [projectId, setProjectId] = useState('novels6')
  const [config, setConfig] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setLoading(true)
    novelApi.getConfig(projectId).then((data) => {
      setConfig(data)
      setLoading(false)
    })
  }, [projectId])

  const handleSave = async () => {
    setSaving(true)
    try {
      await novelApi.saveConfig(projectId, config)
      Toast.success('配置保存成功')
    } finally {
      setSaving(false)
    }
  }

  const updateField = (path, value) => {
    const keys = path.split('.')
    const newConfig = { ...config }
    let obj = newConfig
    for (let i = 0; i < keys.length - 1; i++) {
      obj[keys[i]] = { ...obj[keys[i]] }
      obj = obj[keys[i]]
    }
    obj[keys[keys.length - 1]] = value
    setConfig(newConfig)
  }

  const copyAsNew = () => {
    const newConfig = JSON.stringify(config, null, 2)
    navigator.clipboard?.writeText(newConfig)
    Toast.success('配置已复制到剪贴板')
  }

  if (loading || !config) {
    return (
      <div>
        <Title heading={3} style={{ marginBottom: 16 }}>配置管理</Title>
        <Skeleton active paragraph={{ rows: 12 }} />
      </div>
    )
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <Title heading={3}>配置管理</Title>
        <Space>
          <Select
            value={projectId}
            optionList={PROJECT_OPTIONS}
            onChange={setProjectId}
            style={{ width: 160 }}
          />
          <Button icon={<IconCopy />} onClick={copyAsNew}>复制配置</Button>
          <Button icon={<IconSave />} type="primary" loading={saving} onClick={handleSave}>保存</Button>
        </Space>
      </div>

      <Tabs type="card">
        <TabPane tab="基础配置" itemKey="basic">
          <Card style={{ maxWidth: 800 }}>
            <Form layout="horizontal" labelPosition="left" labelAlign="right" labelWidth={160}>
              <Form.Input
                field="title"
                label="小说标题"
                value={config.title}
                onChange={(v) => updateField('title', v)}
              />
              <Form.Input
                field="project_dir"
                label="项目目录"
                value={config.project_dir}
                onChange={(v) => updateField('project_dir', v)}
              />
              <Form.InputNumber
                field="total_chapters"
                label="总章节数"
                value={config.total_chapters}
                onChange={(v) => updateField('total_chapters', v)}
                min={1} max={5000}
              />
              <Form.Select
                field="model"
                label="模型"
                value={config.model}
                onChange={(v) => updateField('model', v)}
                optionList={[
                  { value: 'MiniMax-M2.7-highspeed', label: 'MiniMax-M2.7-highspeed' },
                  { value: 'MiniMax-M2.7', label: 'MiniMax-M2.7' },
                ]}
              />
              <Form.InputNumber
                field="api_qps"
                label="API QPS"
                value={config.api_qps}
                onChange={(v) => updateField('api_qps', v)}
                step={0.1} min={0.1} max={10}
              />
            </Form>
          </Card>
        </TabPane>

        <TabPane tab="Writer" itemKey="writer">
          <Card style={{ maxWidth: 800 }}>
            <Form layout="horizontal" labelPosition="left" labelAlign="right" labelWidth={160}>
              <Form.InputNumber
                field="writer.max_tokens"
                label="Max Tokens"
                value={config.writer.max_tokens}
                onChange={(v) => updateField('writer.max_tokens', v)}
                min={1024} max={32768}
              />
              <Form.InputNumber
                field="writer.temperature"
                label="Temperature"
                value={config.writer.temperature}
                onChange={(v) => updateField('writer.temperature', v)}
                step={0.1} min={0} max={2}
              />
              <Form.InputNumber
                field="writer.context_chapters"
                label="上下文章节数"
                value={config.writer.context_chapters}
                onChange={(v) => updateField('writer.context_chapters', v)}
                min={0} max={10}
              />
              <Form.InputNumber
                field="writer.max_retries"
                label="最大重试"
                value={config.writer.max_retries}
                onChange={(v) => updateField('writer.max_retries', v)}
                min={0} max={10}
              />
              <Form.InputNumber
                field="writer.retry_delay"
                label="重试延迟(秒)"
                value={config.writer.retry_delay}
                onChange={(v) => updateField('writer.retry_delay', v)}
                step={0.5} min={0}
              />
            </Form>
          </Card>
        </TabPane>

        <TabPane tab="Reviewer" itemKey="reviewer">
          <Card style={{ maxWidth: 800 }}>
            <Form layout="horizontal" labelPosition="left" labelAlign="right" labelWidth={160}>
              <Form.InputNumber
                field="reviewer.max_tokens"
                label="Max Tokens"
                value={config.reviewer.max_tokens}
                onChange={(v) => updateField('reviewer.max_tokens', v)}
              />
              <Form.InputNumber
                field="reviewer.temperature"
                label="Temperature"
                value={config.reviewer.temperature}
                onChange={(v) => updateField('reviewer.temperature', v)}
                step={0.1} min={0} max={2}
              />
              <Form.InputNumber
                field="reviewer.min_score"
                label="最低通过分"
                value={config.reviewer.min_score}
                onChange={(v) => updateField('reviewer.min_score', v)}
                step={0.1} min={0} max={10}
              />
              <Form.InputNumber
                field="reviewer.rewrite_threshold"
                label="重写阈值"
                value={config.reviewer.rewrite_threshold}
                onChange={(v) => updateField('reviewer.rewrite_threshold', v)}
                step={0.1} min={0} max={10}
              />
            </Form>
          </Card>
        </TabPane>

        <TabPane tab="Coordinator" itemKey="coordinator">
          <Card style={{ maxWidth: 800 }}>
            <Form layout="horizontal" labelPosition="left" labelAlign="right" labelWidth={160}>
              <Form.InputNumber
                field="coordinator.batch_size"
                label="批次大小"
                value={config.coordinator.batch_size}
                onChange={(v) => updateField('coordinator.batch_size', v)}
                min={1} max={100}
              />
              <Form.InputNumber
                field="coordinator.num_workers"
                label="Worker 数"
                value={config.coordinator.num_workers}
                onChange={(v) => updateField('coordinator.num_workers', v)}
                min={1} max={20}
              />
              <Form.Switch
                field="coordinator.auto_fill_missing"
                label="自动补全"
                checked={config.coordinator.auto_fill_missing}
                onChange={(v) => updateField('coordinator.auto_fill_missing', v)}
              />
              <Form.Switch
                field="coordinator.auto_rewrite"
                label="自动重写"
                checked={config.coordinator.auto_rewrite}
                onChange={(v) => updateField('coordinator.auto_rewrite', v)}
              />
              <Form.InputNumber
                field="coordinator.pause_between_batches"
                label="批次间隔(秒)"
                value={config.coordinator.pause_between_batches}
                onChange={(v) => updateField('coordinator.pause_between_batches', v)}
                step={0.5} min={0}
              />
            </Form>
          </Card>
        </TabPane>

        <TabPane tab="多媒体" itemKey="multimedia">
          <Card style={{ maxWidth: 800 }}>
            <Form layout="horizontal" labelPosition="left" labelAlign="right" labelWidth={160}>
              <Form.Switch
                field="multimedia.enabled"
                label="启用多媒体"
                checked={config.multimedia.enabled}
                onChange={(v) => updateField('multimedia.enabled', v)}
              />
              <Form.Switch
                field="multimedia.images.enabled"
                label="生成图片"
                checked={config.multimedia.images.enabled}
                onChange={(v) => updateField('multimedia.images.enabled', v)}
              />
              <Form.Switch
                field="multimedia.videos.enabled"
                label="生成视频"
                checked={config.multimedia.videos.enabled}
                onChange={(v) => updateField('multimedia.videos.enabled', v)}
              />
              <Form.Switch
                field="multimedia.audio.enabled"
                label="生成语音"
                checked={config.multimedia.audio.enabled}
                onChange={(v) => updateField('multimedia.audio.enabled', v)}
              />
              <Form.Switch
                field="multimedia.music.enabled"
                label="生成音乐"
                checked={config.multimedia.music.enabled}
                onChange={(v) => updateField('multimedia.music.enabled', v)}
              />
            </Form>
          </Card>
        </TabPane>
      </Tabs>
    </div>
  )
}

export default Config
