# ghmirror side-channel agent. Run with `gitlab-rails runner`; reads one JSON array of
# operations per line on stdin and answers with one GHMIRROR_RESULT line per operation.
require 'json'
$stdout.sync = true
def ts(s)
  s && Time.iso8601(s)
end

def emit(h)
  puts "GHMIRROR_RESULT " + h.to_json
  $stdout.flush
end

def run_op(op, i)
  if op['op'] == 'ping'
    return emit(i: i, ok: true, version: Gitlab::VERSION)
  end
  project = Project.find(op['project_id'])
  case op['op']
  when 'mr_meta'
    mr = project.merge_requests.find_by!(iid: op['iid'])
    cols = {}
    cols[:created_at] = ts(op['created_at']) if op['created_at']
    cols[:updated_at] = ts(op['updated_at']) if op['updated_at']
    cols[:target_branch] = op['target_branch'] if op['target_branch']
    case op['state']
    when 'merged'
      cols[:state_id] = MergeRequest.available_states[:merged]
      cols[:merge_commit_sha] = op['merge_commit_sha'] if op['merge_commit_sha']
    when 'closed'
      cols[:state_id] = MergeRequest.available_states[:closed]
    end
    mr.update_columns(cols) unless cols.empty?
    metrics = mr.metrics || mr.build_metrics
    metrics.target_project_id ||= mr.target_project_id if metrics.respond_to?(:target_project_id=)
    m = {}
    m[:merged_by_id] = op['merged_by_id'] if op.key?('merged_by_id')
    m[:merged_at] = ts(op['merged_at']) if op['merged_at']
    m[:latest_closed_by_id] = op['closed_by_id'] if op['closed_by_id']
    m[:latest_closed_at] = ts(op['closed_at']) if op['closed_at']
    metrics.assign_attributes(m)
    metrics.save!
    emit(i: i, ok: true)
  when 'mr_iid'
    # restore number parity: move an MR to the iid of its GitHub PR (only if that iid is free)
    mr = project.merge_requests.find_by!(iid: op['from_iid'])
    if project.merge_requests.where(iid: op['to_iid']).exists?
      emit(i: i, ok: false, error: "iid #{op['to_iid']} already used")
    else
      old_ref = mr.ref_path
      mr.update_columns(iid: op['to_iid'])
      mr.reload
      sha = mr.merge_request_diff&.head_commit_sha || mr.diff_head_sha
      project.repository.write_ref(mr.ref_path, sha) if sha
      project.repository.delete_refs(old_ref) rescue nil
      emit(i: i, ok: true)
    end
  when 'issue_meta'
    issue = project.issues.find_by!(iid: op['iid'])
    cols = {}
    cols[:created_at] = ts(op['created_at']) if op['created_at']
    cols[:updated_at] = ts(op['updated_at']) if op['updated_at']
    cols[:closed_at] = ts(op['closed_at']) if op['closed_at']
    cols[:closed_by_id] = op['closed_by_id'] if op['closed_by_id']
    issue.update_columns(cols) unless cols.empty?
    emit(i: i, ok: true)
  when 'approval'
    mr = project.merge_requests.find_by!(iid: op['iid'])
    approval = mr.approvals.find_or_initialize_by(user_id: op['user_id'])
    approval.project_id = mr.target_project_id if approval.respond_to?(:project_id=) && approval.project_id.nil?
    approval.save!(validate: false) if approval.new_record?
    approval.update_columns(created_at: ts(op['created_at']), updated_at: ts(op['created_at'])) if op['created_at']
    emit(i: i, ok: true, approval_id: approval.id)
  when 'unapprove'
    mr = project.merge_requests.find_by!(iid: op['iid'])
    n = mr.approvals.where(user_id: op['user_id']).delete_all
    emit(i: i, ok: true, removed: n)
  when 'release_meta'
    rel = project.releases.find_by!(tag: op['tag'])
    cols = {}
    cols[:author_id] = op['author_id'] if op['author_id']
    cols[:created_at] = ts(op['created_at']) if op['created_at']
    rel.update_columns(cols) unless cols.empty?
    emit(i: i, ok: true)
  when 'note_meta'
    note = project.notes.find(op['note_id'])
    note.update_columns(updated_at: ts(op['updated_at'])) if op['updated_at']
    emit(i: i, ok: true)
  else
    emit(i: i, ok: false, error: "unknown op #{op['op']}")
  end
rescue => e
  emit(i: i, ok: false, error: "#{e.class}: #{e.message}"[0, 500])
end

# ARGV: [token, idle_seconds]. The token only makes this process findable for `pkill -f`.
# Exiting after idle_seconds without input means an engine that died (or a docker exec / ssh
# client that was killed without closing our stdin) can never leave this process behind.
idle = (ARGV[1] || '900').to_i
puts 'GHMIRROR_READY'
while IO.select([$stdin], nil, nil, idle) && (line = $stdin.gets)
  begin
    JSON.parse(line).each_with_index { |op, i| run_op(op, i) }
  rescue JSON::ParserError => e
    emit(i: -1, ok: false, error: e.message)
  end
  puts 'GHMIRROR_BATCH_END'
end
