# Receipt fields

`task_id`, `task_snapshot_sha256`, `source_commit`, `image_digest`, `harness`,
`runtime`, `self_test`, `cluster_policy`, `resources`, `verifier_result`,
`trajectory_paths`, `created_at`, and `status` are the minimum fields. Store
the receipt in the workspace state directory and keep secrets out of it.
