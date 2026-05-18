import React, { useEffect, useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Table, Pagination, Select, Space, Tag, Typography,
  Button, Input, Skeleton, Toast,
} from '@douyinfe/semi-ui'
import { IconSearch, IconRefresh } from '@douyinfe/semi-icons'
import { novelApi } from '../api/novelApi'

const { Title, Text } = Typography

const PROJECT_OPTIONS = [
  { value: 'novels1', label: 'novels1' },
  { value: 'novels2', label: 'novels2 (剑来风格)' },
  { value: 'novels3', label: 'novels3 (武道通神)' },
  { value: 'novels4', label: 'novels4' },
  { value: 'novels5', label: 'novels5' },
  { value: 'novels6', label: 'novels6 (新书示例)' },
  { value: 'novels7', label: 'novels7 (凡尘逆仙)' },
]

const STATUS_TAG = {
  pending: <Tag color="grey">待生成</Tag>,
  draft: <Tag color="blue">初稿</Tag>,
  final: <Tag color="green">终稿</Tag>,
}

function Chapters() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [projectId, setProjectId] = useState(searchParams.get('project') || 'novels6')
  const [data, setData] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [searchText, setSearchText] = useState('')

  const fetchChapters = useCallback(async () => {
    setLoading(true)
    try {
      const res = await novelApi.getChapters(projectId, page, pageSize)
      setData(res.chapters)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [projectId, page, pageSize])

  useEffect(() => {
    fetchChapters()
  }, [fetchChapters])

  const handleProjectChange = (value) => {
    setProjectId(value)
    setPage(1)
    setSearchParams({ project: value })
  }

  const handleBatchAction = (action) => {
    Toast.info(`${action} 功能待实现`)
  }

  const columns = [
    {
      title: '章节',
      dataIndex: 'id',
      width: 80,
      render: (id) => `第${id}章`,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (status) => STATUS_TAG[status] || status,
    },
    {
      title: '字数',
      dataIndex: 'wordCount',
      width: 100,
      render: (v) => v > 0 ? v.toLocaleString() : '-',
    },
    {
      title: '评分',
      dataIndex: 'score',
      width: 100,
      render: (v) => v ? (
        <Text type={v >= 7 ? 'success' : v >= 6 ? 'warning' : 'danger'} strong>{v}</Text>
      ) : '-',
    },
    {
      title: '初稿',
      dataIndex: 'hasDraft',
      width: 80,
      render: (v) => v ? <Tag color="green" size="small">有</Tag> : <Tag size="small">无</Tag>,
    },
    {
      title: '终稿',
      dataIndex: 'hasFinal',
      width: 80,
      render: (v) => v ? <Tag color="green" size="small">有</Tag> : <Tag size="small">无</Tag>,
    },
    {
      title: '评审',
      dataIndex: 'hasReview',
      width: 80,
      render: (v) => v ? <Tag color="green" size="small">有</Tag> : <Tag size="small">无</Tag>,
    },
    {
      title: '操作',
      width: 120,
      render: (_, record) => (
        <Button size="small" disabled={!record.hasDraft}>查看</Button>
      ),
    },
  ]

  const filteredData = data.filter((ch) => {
    if (!searchText) return true
    return ch.title.includes(searchText)
  })

  return (
    <div>
      <Title heading={3} style={{ marginBottom: 16 }}>章节管理</Title>

      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          value={projectId}
          optionList={PROJECT_OPTIONS}
          onChange={handleProjectChange}
          style={{ width: 200 }}
        />
        <Input
          prefix={<IconSearch />}
          placeholder="搜索章节"
          value={searchText}
          onChange={(v) => setSearchText(v)}
          style={{ width: 200 }}
        />
        <Button icon={<IconRefresh />} onClick={fetchChapters}>刷新</Button>
        <Button onClick={() => handleBatchAction('补全缺失章节')}>补全缺失</Button>
        <Button onClick={() => handleBatchAction('批量评审')}>批量评审</Button>
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
    </div>
  )
}

export default Chapters
