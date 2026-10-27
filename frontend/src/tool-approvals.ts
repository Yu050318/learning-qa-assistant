import type {CapabilityDirectory,CapabilityTool} from './api'

export type ToolApprovalDraft={skillName:string;revision:number;tools:CapabilityTool[]}

export function sandboxBudget(tool:CapabilityTool):[string,string][]{
  if(tool.resource_scope!=='isolated-workspace')return []
  const limits=tool.sandbox_limits
  const keys=['timeout_seconds','memory_bytes','cpu_count','pids_limit','output_bytes','workspace_bytes','max_files','max_file_bytes','max_artifact_bytes'] as const
  if(!limits||keys.some(key=>!Number.isFinite(limits[key])||limits[key]<=0)||
    keys.filter(key=>key!=='timeout_seconds'&&key!=='cpu_count').some(key=>!Number.isSafeInteger(limits[key]))||
    limits.max_file_bytes>limits.max_artifact_bytes||limits.max_artifact_bytes>limits.workspace_bytes)
    throw new Error('隔离执行资源限额不完整，请刷新后重新核对')
  const size=(bytes:number)=>bytes>=1048576?`${+(bytes/1048576).toFixed(2)} MiB`:`${+(bytes/1024).toFixed(2)} KiB`
  return [['执行时间',`${limits.timeout_seconds} 秒`],['内存',size(limits.memory_bytes)],['CPU',`${limits.cpu_count} 核`],
    ['进程数',`${limits.pids_limit}`],['临时空间',size(limits.workspace_bytes)],['输出',size(limits.output_bytes)],
    ['生成文件',`最多 ${limits.max_files} 份 · 单份 ${size(limits.max_file_bytes)} · 合计 ${size(limits.max_artifact_bytes)}`]]
}

export function requestedSkill(question:string,selected:string|null){
  const slash=/^\/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)/.exec(question.trim())?.[1]
  if(!slash||slash==='remember')return selected
  if(selected&&selected!==slash)throw new Error('斜杠命令与所选 Skill 不一致')
  return slash
}

export function toolApprovalDraft(directory:CapabilityDirectory|null,role:string|undefined,enabled:boolean,name:string|null):ToolApprovalDraft|null{
  if(!name)return null
  const skill=directory?.skills.find(item=>item.name===name)
  if(!skill||!skill.available)throw new Error('该 Skill 已停用、未配置或当前账号无权使用，请重新选择')
  if(!skill.approval_tools.length)return null
  if(!enabled)throw new Error('工具审批尚未开放')
  if(role!=='admin')throw new Error('只有平台管理员可批准高风险工具')
  const names=[...new Set(skill.approval_tools)].sort()
  const tools=names.map(name=>directory!.tools.find(tool=>tool.name===name))
  if(tools.some(tool=>!tool?.available||!tool.requires_approval))throw new Error('工具状态已变化，请刷新后重新核对')
  tools.forEach(tool=>sandboxBudget(tool!))
  return {skillName:skill.name,revision:directory!.revision,tools:tools as CapabilityTool[]}
}
