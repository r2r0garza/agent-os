"use client"

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  CheckCircle2,
  FileOutput,
  FileText,
  Link2,
  LoaderCircle,
  RefreshCw,
  Upload,
} from "lucide-react"

import {
  Artifact,
  ArtifactCitation,
  ArtifactLineage,
  api,
  apiText,
  jsonBody,
} from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"

interface ArtifactWorkspaceProps {
  projectId: string
  goalId: string
  artifacts: Artifact[]
  onRefresh: () => Promise<void>
}

interface ArtifactDetail {
  lineage: ArtifactLineage
  content: string | null
  contentError: string
  citations: ArtifactCitation[]
}

function displayDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function displayBytes(value: number | undefined) {
  if (value === undefined) return "No version"
  if (value < 1_024) return `${value} B`
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KiB`
  return `${(value / 1_048_576).toFixed(1)} MiB`
}

function statusTone(artifact: Artifact) {
  if (
    artifact.ingestion_status === "unsupported" ||
    artifact.ingestion_status === "failed" ||
    artifact.ingestion_status === "needs_reconciliation" ||
    artifact.latest_version?.storage_state !== "finalized"
  ) {
    return "destructive" as const
  }
  return "outline" as const
}

function readCitations(content: string, artifact: Artifact) {
  if (artifact.kind !== "output") return []
  try {
    const payload = JSON.parse(content) as { citations?: unknown }
    if (!Array.isArray(payload.citations)) return []
    return payload.citations.filter(
      (citation): citation is ArtifactCitation =>
        typeof citation === "object" &&
        citation !== null &&
        typeof (citation as ArtifactCitation).source_artifact_id === "string" &&
        typeof (citation as ArtifactCitation).normalized_artifact_id ===
          "string"
    )
  } catch {
    return []
  }
}

function ArtifactLink({ artifact }: { artifact: Artifact }) {
  return (
    <div className="rounded-lg border bg-muted/20 p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="truncate text-sm font-medium">{artifact.name}</p>
        <Badge variant="outline">{artifact.kind}</Badge>
      </div>
      <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">
        {artifact.id}
      </p>
    </div>
  )
}

export function ArtifactWorkspace({
  projectId,
  goalId,
  artifacts,
  onRefresh,
}: ArtifactWorkspaceProps) {
  const [selectedArtifactId, setSelectedArtifactId] = useState("")
  const [detail, setDetail] = useState<ArtifactDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState("")
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState("")
  const [uploadNotice, setUploadNotice] = useState("")

  const visibleArtifacts = useMemo(
    () =>
      artifacts.filter(
        (artifact) =>
          !goalId || artifact.goal_id === goalId || artifact.goal_id === null
      ),
    [artifacts, goalId]
  )
  const selectedArtifact =
    visibleArtifacts.find((artifact) => artifact.id === selectedArtifactId) ??
    visibleArtifacts.at(-1) ??
    null

  const loadDetail = useCallback(async (artifact: Artifact) => {
    setDetailLoading(true)
    setDetailError("")
    try {
      const lineage = await api<ArtifactLineage>(
        `/artifacts/${artifact.id}/lineage`
      )
      let content: string | null = null
      let contentError = ""
      try {
        content = await apiText(`/artifacts/${artifact.id}/content`)
      } catch (reason) {
        contentError =
          reason instanceof Error
            ? reason.message
            : "Artifact content is unavailable"
      }
      setDetail({
        lineage,
        content,
        contentError,
        citations: content ? readCitations(content, artifact) : [],
      })
    } catch (reason) {
      setDetail(null)
      setDetailError(
        reason instanceof Error
          ? reason.message
          : "Unable to load artifact detail"
      )
    } finally {
      setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!selectedArtifact) {
      const timer = window.setTimeout(() => setDetail(null), 0)
      return () => window.clearTimeout(timer)
    }
    const timer = window.setTimeout(() => void loadDetail(selectedArtifact), 0)
    return () => window.clearTimeout(timer)
  }, [loadDetail, selectedArtifact])

  async function uploadKnowledge(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!projectId) return
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    setUploading(true)
    setUploadError("")
    setUploadNotice("")
    try {
      const artifact = await api<Artifact>(
        `/projects/${projectId}/artifacts`,
        jsonBody({
          name: form.get("name"),
          content: form.get("content"),
          content_type: form.get("content_type"),
        })
      )
      setSelectedArtifactId(artifact.id)
      setUploadNotice(
        artifact.ingestion_status === "complete"
          ? "Source preserved and normalized knowledge is ready."
          : `Source preserved with ${artifact.ingestion_status} ingestion status.`
      )
      formElement.reset()
      await onRefresh()
    } catch (reason) {
      setUploadError(reason instanceof Error ? reason.message : "Upload failed")
    } finally {
      setUploading(false)
    }
  }

  const citedArtifacts = (detail?.citations ?? []).map((citation) => ({
    citation,
    source: artifacts.find(
      (artifact) => artifact.id === citation.source_artifact_id
    ),
    normalized: artifacts.find(
      (artifact) => artifact.id === citation.normalized_artifact_id
    ),
  }))

  return (
    <section className="mb-6 grid gap-6 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <Upload className="size-4" /> PROJECT KNOWLEDGE
          </div>
          <CardTitle>Upload durable source material</CardTitle>
          <CardDescription>
            Text and Markdown are preserved as immutable source artifacts and
            normalized for bounded agent consumption.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-5">
          <form className="grid gap-4" onSubmit={uploadKnowledge}>
            <div className="grid gap-1.5">
              <Label htmlFor="artifact-name">File name</Label>
              <Input
                id="artifact-name"
                name="name"
                placeholder="project-brief.md"
                required
                disabled={!projectId || uploading}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="artifact-content-type">Content type</Label>
              <select
                id="artifact-content-type"
                name="content_type"
                className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                defaultValue="text/markdown"
                disabled={!projectId || uploading}
              >
                <option value="text/markdown">Markdown</option>
                <option value="text/plain">Plain text</option>
                <option value="application/pdf">
                  Unsupported format smoke check
                </option>
              </select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="artifact-content">Source content</Label>
              <Textarea
                id="artifact-content"
                name="content"
                className="min-h-36 font-mono text-xs"
                placeholder="# Project brief\n\nDurable source material..."
                required
                disabled={!projectId || uploading}
              />
            </div>
            <Button
              type="submit"
              className="justify-self-end"
              disabled={!projectId || uploading}
            >
              {uploading ? (
                <LoaderCircle className="animate-spin" />
              ) : (
                <Upload />
              )}
              {uploading ? "Uploading…" : "Upload knowledge"}
            </Button>
          </form>

          {uploadError ? (
            <div className="flex gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <span>{uploadError} Check the backend connection and retry.</span>
            </div>
          ) : null}
          {uploadNotice ? (
            <div className="flex gap-2 rounded-xl border bg-muted/20 p-3 text-sm">
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600" />
              <span>{uploadNotice}</span>
            </div>
          ) : null}

          <div className="grid gap-2">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-medium">Persisted artifacts</p>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void onRefresh()}
              >
                <RefreshCw /> Refresh
              </Button>
            </div>
            {visibleArtifacts.length ? (
              <div className="grid max-h-96 gap-2 overflow-y-auto pr-1">
                {[...visibleArtifacts].reverse().map((artifact) => (
                  <button
                    key={artifact.id}
                    type="button"
                    className={`grid gap-2 rounded-xl border p-3 text-left transition-colors hover:bg-muted/40 ${
                      selectedArtifact?.id === artifact.id
                        ? "border-foreground bg-muted/30"
                        : ""
                    }`}
                    onClick={() => setSelectedArtifactId(artifact.id)}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">
                        {artifact.name}
                      </span>
                      <Badge variant="outline">{artifact.kind}</Badge>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                      <Badge variant={statusTone(artifact)}>
                        {artifact.ingestion_status}
                      </Badge>
                      <span>{artifact.content_type ?? "unknown type"}</span>
                      <span>
                        {displayBytes(artifact.latest_version?.size_bytes)}
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <div className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
                Select a project and upload its first knowledge artifact.
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <FileText className="size-4" /> ARTIFACT INSPECTOR
          </div>
          <CardTitle>
            {selectedArtifact?.name ?? "Select an artifact"}
          </CardTitle>
          <CardDescription>
            Inspect immutable version metadata, content availability, lineage,
            and output citations.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {detailLoading ? (
            <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
              <LoaderCircle className="size-4 animate-spin" /> Loading persisted
              artifact detail…
            </div>
          ) : detailError ? (
            <div className="flex items-start justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
              <span>{detailError}</span>
              {selectedArtifact ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void loadDetail(selectedArtifact)}
                >
                  Retry
                </Button>
              ) : null}
            </div>
          ) : selectedArtifact && detail ? (
            <div className="grid gap-5">
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-xl border p-3">
                  <p className="text-xs text-muted-foreground">Type & state</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Badge variant="outline">{selectedArtifact.kind}</Badge>
                    <Badge variant={statusTone(selectedArtifact)}>
                      {selectedArtifact.ingestion_status}
                    </Badge>
                    <Badge variant={statusTone(selectedArtifact)}>
                      {selectedArtifact.latest_version?.storage_state ??
                        "no version"}
                    </Badge>
                  </div>
                </div>
                <div className="rounded-xl border p-3">
                  <p className="text-xs text-muted-foreground">
                    Immutable version
                  </p>
                  <p className="mt-2 text-sm font-medium">
                    v{selectedArtifact.latest_version?.version_number ?? "—"} ·{" "}
                    {displayBytes(selectedArtifact.latest_version?.size_bytes)}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {displayDate(selectedArtifact.created_at)}
                  </p>
                </div>
              </div>

              <div className="rounded-xl border p-3">
                <p className="text-xs text-muted-foreground">Content hash</p>
                <p className="mt-2 font-mono text-xs break-all">
                  {selectedArtifact.latest_version?.content_hash ??
                    "No committed version"}
                </p>
              </div>

              {selectedArtifact.ingestion_error ? (
                <div className="flex gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                  <span>{selectedArtifact.ingestion_error}</span>
                </div>
              ) : null}

              {Object.keys(selectedArtifact.ingestion_metadata).length ? (
                <div className="grid gap-2">
                  <p className="text-sm font-medium">Ingestion metadata</p>
                  <pre className="max-h-48 overflow-auto rounded-xl border bg-muted/20 p-3 text-xs whitespace-pre-wrap">
                    {JSON.stringify(
                      selectedArtifact.ingestion_metadata,
                      null,
                      2
                    )}
                  </pre>
                </div>
              ) : null}

              <div className="grid gap-2">
                <p className="flex items-center gap-2 text-sm font-medium">
                  <Link2 className="size-4" /> Lineage
                </p>
                {detail.lineage.parent ? (
                  <ArtifactLink artifact={detail.lineage.parent} />
                ) : null}
                <ArtifactLink artifact={detail.lineage.artifact} />
                {detail.lineage.children.map((child) => (
                  <ArtifactLink key={child.id} artifact={child} />
                ))}
                {!detail.lineage.parent && !detail.lineage.children.length ? (
                  <p className="text-xs text-muted-foreground">
                    No parent or child artifacts recorded.
                  </p>
                ) : null}
              </div>

              {selectedArtifact.kind === "output" ? (
                <div className="grid gap-2">
                  <p className="flex items-center gap-2 text-sm font-medium">
                    <FileOutput className="size-4" /> Citations
                  </p>
                  {citedArtifacts.length ? (
                    citedArtifacts.map(({ citation, source, normalized }) => (
                      <div
                        key={`${citation.source_artifact_id}:${citation.normalized_artifact_id}`}
                        className="rounded-xl border p-3 text-sm"
                      >
                        <p className="font-medium">
                          {source?.name ?? citation.source_artifact_id}
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          Normalized through{" "}
                          {normalized?.name ?? citation.normalized_artifact_id}
                        </p>
                        <pre className="mt-2 overflow-x-auto rounded-lg bg-muted/40 p-2 text-[10px]">
                          {JSON.stringify(citation.citation_anchor, null, 2)}
                        </pre>
                      </div>
                    ))
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      This output contains no project-knowledge citations.
                    </p>
                  )}
                </div>
              ) : null}

              <div className="grid gap-2">
                <p className="text-sm font-medium">Content preview</p>
                {detail.contentError ? (
                  <div className="flex gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                    <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                    <span>
                      {detail.contentError}. Reconcile storage, then refresh
                      this artifact.
                    </span>
                  </div>
                ) : (
                  <pre className="max-h-72 overflow-auto rounded-xl border bg-muted/20 p-3 text-xs whitespace-pre-wrap">
                    {detail.content || "The finalized artifact is empty."}
                  </pre>
                )}
              </div>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
              Upload or select an artifact to inspect real backend state.
            </div>
          )}
        </CardContent>
      </Card>
    </section>
  )
}
