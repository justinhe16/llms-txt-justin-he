-- CreateEnum
CREATE TYPE "publish_mode" AS ENUM ('pull_request', 'commit');

-- CreateEnum
CREATE TYPE "publish_status" AS ENUM ('skipped_unchanged', 'succeeded', 'failed');

-- CreateTable
CREATE TABLE "github_installations" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "user_id" UUID NOT NULL,
    "installation_id" BIGINT NOT NULL,
    "account_login" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "github_installations_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "publish_targets" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "website_id" UUID NOT NULL,
    "installation_row_id" UUID NOT NULL,
    "repo_owner" TEXT NOT NULL,
    "repo_name" TEXT NOT NULL,
    "base_branch" TEXT NOT NULL,
    "path" TEXT NOT NULL DEFAULT 'llms.txt',
    "mode" "publish_mode" NOT NULL DEFAULT 'pull_request',
    "active" BOOLEAN NOT NULL DEFAULT false,

    CONSTRAINT "publish_targets_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "publications" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "target_id" UUID NOT NULL,
    "status" "publish_status" NOT NULL,
    "commit_sha" TEXT,
    "pr_url" TEXT,
    "pr_number" INTEGER,
    "error" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "publications_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "github_installations_user_id_idx" ON "github_installations"("user_id");

-- CreateIndex
CREATE UNIQUE INDEX "github_installations_user_id_installation_id_key" ON "github_installations"("user_id", "installation_id");

-- CreateIndex
CREATE UNIQUE INDEX "publish_targets_website_id_key" ON "publish_targets"("website_id");

-- CreateIndex
CREATE INDEX "publish_targets_installation_row_id_idx" ON "publish_targets"("installation_row_id");

-- CreateIndex
CREATE INDEX "publications_target_id_created_at_idx" ON "publications"("target_id", "created_at" DESC);

-- CreateIndex
CREATE INDEX "publications_run_id_idx" ON "publications"("run_id");

-- AddForeignKey
ALTER TABLE "publish_targets" ADD CONSTRAINT "publish_targets_website_id_fkey" FOREIGN KEY ("website_id") REFERENCES "websites"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "publish_targets" ADD CONSTRAINT "publish_targets_installation_row_id_fkey" FOREIGN KEY ("installation_row_id") REFERENCES "github_installations"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "publications" ADD CONSTRAINT "publications_run_id_fkey" FOREIGN KEY ("run_id") REFERENCES "runs"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "publications" ADD CONSTRAINT "publications_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "publish_targets"("id") ON DELETE CASCADE ON UPDATE CASCADE;
