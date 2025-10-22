#!/usr/bin/env node
/**
 * Build E2B template using the SDK (alternative to CLI)
 *
 * Usage:
 *   export E2B_API_KEY=your-key
 *   node examples/e2b/build_template.mjs
 */

import { Template } from 'e2b';

async function buildTemplate() {
  console.log('Building Nexus E2B template...');

  const template = Template()
    .fromDockerfile('./e2b.Dockerfile')
    .setEnvs({
      NEXUS_URL: 'http://nexus.sudorouter.ai',
    });

  try {
    await Template.build(template, {
      alias: 'nexus-sandbox-v1',
      cpuCount: 2,
      memoryMB: 2048,
      onBuildLogs: (log) => console.log(log.message),
    });

    console.log('✓ Template built successfully: nexus-sandbox-v1');
  } catch (error) {
    console.error('✗ Template build failed:', error);
    process.exit(1);
  }
}

buildTemplate();
