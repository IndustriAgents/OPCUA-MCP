// Validate administrator CA chains and signed offline CRLs before a peer session.
import { webcrypto, X509Certificate } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { isIP } from "node:net";
import { join } from "node:path";
import { InMemoryCertificateStore } from "node-opcua-common";
import { exploreCertificate, split_der } from "node-opcua-crypto";
import { StatusCodes } from "node-opcua-status-code";
import {
  Certificate,
  CertificateChainValidationEngine,
  CertificateRevocationList,
  ChainValidationCode,
  CryptoEngine,
} from "pkijs";

const cryptoEngine = new CryptoEngine({ name: "node", crypto: webcrypto as unknown as Crypto });
const REMEDIATION =
  "Check the configured CA chain, current signed CRLs, endpoint hostname and " +
  "OPCUA_SERVER_APPLICATION_URI; update the trust store before reconnecting.";
export const trustRefusal = (status: string) =>
  `OPCUA_SERVER_TRUST_STORE: ${status}. ${REMEDIATION}`;

function files(root: string, folder: string): Buffer[] {
  const directory = join(root, folder);
  if (!existsSync(directory)) return [];
  const paths = readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => join(directory, entry.name));
  if (paths.length > 100 || paths.some((path) => statSync(path).size > 1024 * 1024)) {
    throw new Error("invalid bounded trust material");
  }
  return paths.map((path) => readFileSync(path));
}

function crl(data: Buffer): CertificateRevocationList {
  const raw = data.toString("ascii").startsWith("-----BEGIN")
    ? Buffer.from(
        data.toString("ascii").replace(/-----BEGIN X509 CRL-----|-----END X509 CRL-----|\s/g, ""),
        "base64"
      )
    : data;
  return CertificateRevocationList.fromBER(Uint8Array.from(raw).buffer);
}

export interface TrustIdentity {
  applicationUri: string;
  endpoint: string;
  advertisedUri: string;
}

function identityProblem(cert: Buffer, identity: TrustIdentity): string | null {
  const names = exploreCertificate(cert).tbsCertificate.extensions?.subjectAltName;
  if (
    identity.advertisedUri !== identity.applicationUri ||
    !names?.uniformResourceIdentifier?.includes(identity.applicationUri)
  ) {
    return "BadCertificateUriInvalid";
  }
  const hostname = new URL(identity.endpoint).hostname.replace(/^\[|\]$/g, "");
  const x509 = new X509Certificate(cert);
  const match = isIP(hostname)
    ? x509.checkIP(hostname)
    : x509.checkHost(hostname, { subject: "never", wildcards: false });
  return match ? null : "BadCertificateHostNameInvalid";
}

export async function certificateProblem(
  path: string,
  certificate: Buffer | Buffer[],
  identity?: TrustIdentity,
  now = new Date()
): Promise<string | null> {
  try {
    const anchors = files(path, "trusted/certs").map((data) => new X509Certificate(data).raw);
    const issuers = files(path, "issuers/certs").map((data) => new X509Certificate(data).raw);
    if (!anchors.length) return "BadCertificateUntrusted";
    const authorities = [...anchors, ...issuers];
    if (authorities.some((data) => !new X509Certificate(data).ca)) {
      return "BadCertificateInvalid";
    }
    const peer = Array.isArray(certificate) ? certificate[0] : split_der(certificate)[0];
    if (!peer || peer.length > 1024 * 1024) return "BadCertificateInvalid";
    const leaf = Certificate.fromBER(Uint8Array.from(peer).buffer);
    const cas = authorities.map((data) => Certificate.fromBER(data));
    const crls = [...files(path, "trusted/crl"), ...files(path, "issuers/crl")].map(crl);
    if (
      !crls.length ||
      crls.some(
        (list) => !list.nextUpdate || list.thisUpdate.value > now || list.nextUpdate.value <= now
      )
    ) {
      return "BadCertificateRevocationUnknown";
    }
    for (const list of crls) {
      let valid = false;
      for (let index = 0; index < cas.length; index++) {
        if (
          list.issuer.isEqual(cas[index].subject) &&
          exploreCertificate(authorities[index]).tbsCertificate.extensions?.keyUsage?.cRLSign &&
          (await list.verify({ issuerCertificate: cas[index] }, cryptoEngine))
        )
          valid = true;
      }
      if (!valid) return "BadCertificateInvalid";
    }
    if (leaf.notBefore.value > now || leaf.notAfter.value <= now) {
      return "BadCertificateTimeInvalid";
    }
    if (cas.some((ca) => ca.notBefore.value > now || ca.notAfter.value <= now)) {
      return "BadCertificateIssuerTimeInvalid";
    }
    if (crls.some((list) => list.isCertificateRevoked(leaf))) return "BadCertificateRevoked";
    if (cas.some((ca) => crls.some((list) => list.isCertificateRevoked(ca)))) {
      return "BadCertificateIssuerRevoked";
    }
    const engine = new CertificateChainValidationEngine({
      trustedCerts: anchors.map((data) => Certificate.fromBER(data)),
      certs: [...cas, leaf],
      crls,
      checkDate: now,
    });
    const result = await engine.verify({ passedWhenNotRevValues: false }, cryptoEngine);
    if (!result.result) {
      if (result.resultCode === ChainValidationCode.noRevocation) {
        return "BadCertificateRevocationUnknown";
      }
      if (
        [ChainValidationCode.noPath, ChainValidationCode.noValidPath].includes(result.resultCode)
      ) {
        return "BadCertificateUntrusted";
      }
      return "BadCertificateInvalid";
    }
    if (!result.certificatePath || result.certificatePath.length > 8) {
      return "BadCertificateChainIncomplete";
    }
    return identity ? identityProblem(peer, identity) : null;
  } catch {
    return "BadCertificateInvalid";
  }
}

/** Reload read-only administrator material on every native handshake check. */
export class TrustStore extends InMemoryCertificateStore {
  constructor(
    private readonly path: string,
    private readonly identity: TrustIdentity
  ) {
    super({ autoAcceptUnknown: false });
  }

  override async verifyCertificate(certificate: Buffer | Buffer[]): Promise<string> {
    return (await certificateProblem(this.path, certificate)) ?? "Good";
  }

  override async checkCertificate(certificate: Buffer | Buffer[]) {
    const problem = await certificateProblem(this.path, certificate, this.identity);
    if (problem) throw new Error(trustRefusal(problem));
    return StatusCodes.Good;
  }
}
