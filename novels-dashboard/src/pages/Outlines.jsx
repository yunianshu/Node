import React, { useEffect, useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Table, Pagination, Select, Space, Tag, Typography,
  Button, Input, Skeleton, Toast, SideSheet,
} from '@douyinfe/semi-ui'
import { IconSearch, IconRefresh } from '@douyinfe/semi-icons'
import { novelApi } from '../api/novelApi'

const { Title, Text, Paragraph } = Typography

const PROJECT_OPTIONS = [
  { value: 'novels1', label: 'novels1' },
  { value: 'novels2', label: 'novels2 (剑来风格)' },
  { value: 'novels3', label: 'novels3 (武道通神)' },
  { value: 'novels4', label: 'novels4' },
  { value: 'novels5', label: 'novels5' },
  { value: 'novels6', label: 'novels6 (新书示例)' },
  { value: 'novels7', label: 'novels7 (凡尘逆仙)' },
  { value: 'novels8', label: 'novels8' },
  { value: 'novels9', label: 'novels9' },
  { value: 'novels10', label: 'novels10' },
  { value: 'novels11', label: 'novels11' },
  { value: 'novels12', label: 'novels12' },
]

const VERDICT_TAG = {
  通过: <Tag color="green">通过</Tag>,
  需修改: <Tag color="orange">需修改</Tag>,
  需重写: <Tag color="red">需重写</Tag>,
}

function Outlines() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [projectId, setProjectId] = useState(searchParams.get('project') || 'novels12')
  const [data, setData] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [searchText, setSearchText] = useState('')
  const [selected, setSelected] = useState(null)

  const fetchOutlines = useCallback(async () => {
    setLoading(true)
    try {
      const res = await novelApi.getOutlines(projectId, page, pageSize)
      setData(res.outlines)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [projectId, page, pageSize])

  useEffect(() => {
    fetchOutlines()
  }, [fetchOutlines])

  const handleProjectChange = (value) => {
    setProjectId(value)
    setPage(1)
    setSearchParams({ project: value })
  }

  const handleViewDetail = async (record) => {
    if (!record.hasOutline) {
      Toast.warning('该章节暂无大纲')
      return
    }
    try {
      const detail = await novelApi.getOutline(projectId, record.id)
      setSelected(detail)
    } catch {
      Toast.error('加载大纲详情失败')
    }
  }

  const columns = [
    {
      title: '章节',
      dataIndex: 'id',
      width: 80,
      render: (id) => `第${id}章`,
    },
    {
      title: '标题',
      dataIndex: 'title',
      width: 180,
      render: (title, record) => (
        <Text type={record.hasOutline ? undefined : 'tertiary'}>{title}</Text>
      ),
    },
    {
      title: '摘要',
      dataIndex: 'summary',
      ellipsis: true,
    },
    {
      title: '关键事件',
      dataIndex: 'keyEvents',
      width: 140,
      render: (events) => (
        <Space wrap>
          {events.slice(0, 2).map((evt, idx) => (
            <Tag key={idx} size="small">{evt}</Tag>
          ))}
          {events.length > 2 && <Tag size="small">+{events.length - 2}</Tag>}
        </Space>
      ),
    },
    {
      title: '评分',
      dataIndex: 'score',
      width: 90,
      render: (v) => v ? (
        <Text type={parseFloat(v) >= 8 ? 'success' : parseFloat(v) >= 7 ? 'warning' : 'danger'} strong>{v}</Text>
      ) : '-',
    },
    {
      title: ' verdict',
      dataIndex: 'verdict',
      width: 100,
      render: (verdict) => VERDICT_TAG[verdict] || verdict || '-',
    },
    {
      title: '操作',
      width: 100,
      render: (_, record) => (
        <Button size="small" onClick={() => handleViewDetail(record)}>
          查看
        </Button>
      ),
    },
  ]

  const filteredData = data.filter((item) => {
    if (!searchText) return true
    const text = `${item.title} ${item.summary} ${item.keyEvents.join(' ')}`
    return text.toLowerCase().includes(searchText.toLowerCase())
  })

  return (
    <div>
      <Title heading={3} style={{ marginBottom: 16 }}>大纲查看</Title>

      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          value={projectId}
          optionList={PROJECT_OPTIONS}
          onChange={handleProjectChange}
          style={{ width: 200 }}
        />
        <Input
          prefix={<IconSearch />}
          placeholder="搜索标题或摘要"
          value={searchText}
          onChange={(v) => setSearchText(v)}
          style={{ width: 220 }}
        />
        <Button icon={<IconRefresh />} onClick={fetchOutlines}>刷新</Button>
      </Space>

      {loading ? (
        <Skeleton active paragraph={{ rows: 10 }} />
      ) : (
        <>
          <Table
            columns={columns}
            dataSource={filteredData}
            pagination={false}
            size="small"
          />
          <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
            <Pagination
              total={total}
              pageSize={pageSize}
              currentPage={page}
              onChange={(p) => setPage(p)}
              onPageSizeChange={(s) => { setPageSize(s); setPage(1) }}
              pageSizeOpts={[10, 20, 50, 100]}
              showSizeChanger
            />
          </div>
        </>
      )}

      <SideSheet
        title={selected ? selected.title : '大纲详情'}
        visible={!!selected}
        onCancel={() => setSelected(null)}
        width={560}
      >
        {selected && (
          <Space vertical align="start" style={{ width: '100%' }}>
            <div>
              <Text type="secondary">章节</Text>
              <div><Text strong>第{selected.id}章</Text></div>
            </div>
            <div>
              <Text type="secondary">评分</Text>
              <div>
                <Text strong style={{ marginRight: 8 }}>{selected.score}</Text>
                {VERDICT_TAG[selected.verdict] || selected.verdict}
              </div>
            </div>
            <div>
              <Text type="secondary">摘要</Text>
              <Paragraph>{selected.summary}</Paragraph>
            </div>
            <div>
              <Text type="secondary">关键事件</Text>
              <Space vertical align="start" style={{ marginTop: 8 }}>
                {selected.keyEvents.map((evt, idx) => (
                  <Tag key={idx} size="large">{idx + 1}. {evt}</Tag>
                ))}
              </Space>
            </div>
            <div>
              <Text type="secondary">伏笔</Text>
              <Paragraph>{selected.foreshadowing || '-'}</Paragraph>
            </div>
            <div>
              <Text type="secondary">战力/能力成长</Text>
              <Paragraph>{selected.powerProgression || '-'}</Paragraph>
            </div>
            <div>
              <Text type="secondary">情绪弧线</Text>
              <Paragraph>{selected.emotionalArc || '-'}</Paragraph>
            </div>
            <div>
              <Text type="secondary">章末钩子</Text>
              <Paragraph>{selected.hook || '-'}</Paragraph>
            </div>
          </Space>
        )}
      </SideSheet>
    </div>
  )
}

export default Outlines
