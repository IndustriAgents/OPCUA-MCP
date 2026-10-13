/** ApplicationDescription and requested channel lifetime shared by both runtimes. */
export const CLIENT_APPLICATION_NAME = "OPC UA MCP Client";

export function applicationUriProblem(configured?: string, certificateUri?: string): string | null {
  if (!configured || !certificateUri || configured === certificateUri) return null;
  return `OPCUA_APPLICATION_URI=${configured} does not match the subjectAltName URI of OPCUA_CLIENT_CERT (${certificateUri}). Unset OPCUA_APPLICATION_URI or use the certificate's URI; refusing to connect with conflicting application identities.`;
}
