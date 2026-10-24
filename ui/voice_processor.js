// Capture in the audio rendering thread, independent of mobile page rendering.
class LegalMicrophone extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Float32Array(4096);
    this.used = 0;
  }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        this.samples[this.used++] = channel[i];
        if (this.used === this.samples.length) {
          this.port.postMessage(this.samples, [this.samples.buffer]);
          this.samples = new Float32Array(4096);
          this.used = 0;
        }
      }
    }
    // Output remains silent; only the server's answer is played to the user.
    return true;
  }
}
registerProcessor('legal-microphone', LegalMicrophone);
