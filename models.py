import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence as pack, pack_padded_sequence, pad_packed_sequence
from torch.nn.utils.rnn import pad_packed_sequence as unpack


def reshape_state(state):
    h_state = state[0]
    c_state = state[1]
    new_h_state = torch.cat([h_state[:-1], h_state[1:]], dim=2)
    new_c_state = torch.cat([c_state[:-1], c_state[1:]], dim=2)
    return (new_h_state, new_c_state)


class BahdanauAttention(nn.Module):
    """
    Bahdanau attention mechanism:
    score(h_i, s_j) = v^T * tanh(W_h h_i + W_s s_j)
    """

    def __init__(self, hidden_size):
        super(BahdanauAttention, self).__init__()

        # Initialize the weights and the scoring vector
        self.W_h = nn.Linear(hidden_size, hidden_size, bias=False)  # For encoder hidden states
        self.W_s = nn.Linear(hidden_size, hidden_size, bias=False)  # For decoder hidden states
        self.v = nn.Parameter(torch.randn(hidden_size) * 0.01)  # Scoring vector
        self.layer_norm = nn.LayerNorm(hidden_size)

        # Output linear transformation
        self.W_out = nn.Linear(2 * hidden_size, hidden_size)

    def forward(self, query, encoder_outputs, src_lengths):
        """
        query:          (batch_size, max_tgt_len, hidden_size)
        encoder_outputs:(batch_size, max_src_len, hidden_size)
        src_lengths:    (batch_size)
        Returns:
            attn_out:   (batch_size, max_tgt_len, hidden_size) - attended vector
        """

        batch_size, max_tgt_len, _ = query.size()
        max_src_len = encoder_outputs.size(1)

        # 1. Calculate the alignment scores (batch_size, max_tgt_len, max_src_len)
        score = torch.tanh(self.W_s(query).unsqueeze(2) + self.W_h(encoder_outputs).unsqueeze(1))
        score = torch.einsum('btsh,h->bts', score, self.v)

        # 2. Mask padding positions
        mask = torch.arange(max_src_len, device=score.device).expand(batch_size, max_src_len) >= src_lengths.unsqueeze(1).to(score.device)
        score = score.masked_fill(mask.unsqueeze(1), float('-inf'))

        # 3. Apply softmax to get attention weights (batch_size, max_tgt_len, max_src_len)
        attn_weights = torch.softmax(score, dim=-1)  # Normalize over the source sequence length

        # 4. Compute the context vector (batch_size, max_tgt_len, hidden_size)
        context = attn_weights.matmul(encoder_outputs)  # Weighted sum of encoder outputs

        # 5. Concatenate context vector and previous decoder hidden state (batch_size, max_tgt_len, 2*hidden_size)
        context_and_query = torch.cat((context, query), dim=-1)

        # 6. Pass through the output layer
        attn_out = torch.tanh(self.W_out(context_and_query))  # (batch_size, max_tgt_len, hidden_size)
        attn_out = self.layer_norm(attn_out)

        return attn_out

    def sequence_mask(self, lengths):
        """
        Creates a boolean mask from sequence lengths.
        True for valid positions, False for padding.
        """
        batch_size = lengths.numel()
        max_len = lengths.max()
        return (torch.arange(max_len, device=lengths.device)
                .unsqueeze(0)
                .repeat(batch_size, 1)
                .lt(lengths.unsqueeze(1)))


class Encoder(nn.Module):
    def __init__(
            self,
            src_vocab_size,
            hidden_size,
            padding_idx,
            dropout,
    ):
        super(Encoder, self).__init__()
        self.hidden_size = hidden_size // 2
        self.dropout = dropout

        self.embedding = nn.Embedding(
            src_vocab_size,
            hidden_size,
            padding_idx=padding_idx,
        )
        self.lstm = nn.LSTM(
            hidden_size,
            self.hidden_size,
            bidirectional=True,
            batch_first=True,
        )
        self.dropout = nn.Dropout(self.dropout)

    def forward(
            self,
            src,
            lengths,
    ):
        # src: (batch_size, max_src_len)
        # lengths: (batch_size)

        # 1. Embed the input sequence
        embedded = self.embedding(src)  # (batch_size, max_src_len, hidden_size)

        # 2. Pack the padded sequence
        lengths = lengths.cpu() # Ensure lengths is on CPU
        packed_input = nn.utils.rnn.pack_padded_sequence(embedded, lengths, batch_first=True, enforce_sorted=False)

        # 3. Pass through LSTM
        packed_output, (hidden, cell) = self.lstm(packed_input)

        # 4. Unpack the output
        enc_output, _ = nn.utils.rnn.pad_packed_sequence(packed_output, batch_first=True)

        return enc_output, (hidden, cell)


class Decoder(nn.Module):
    def __init__(
            self,
            hidden_size,
            tgt_vocab_size,
            attn,
            padding_idx,
            dropout,
    ):
        super(Decoder, self).__init__()
        self.hidden_size = hidden_size
        self.tgt_vocab_size = tgt_vocab_size
        self.dropout = dropout

        self.embedding = nn.Embedding(
            self.tgt_vocab_size, self.hidden_size, padding_idx=padding_idx
        )

        self.dropout = nn.Dropout(self.dropout)
        self.lstm = nn.LSTM(
            self.hidden_size,
            self.hidden_size,
            batch_first=True,
        )

        self.attn = attn

    def forward(
            self,
            tgt,
            dec_state,
            encoder_outputs,
            src_lengths,
    ):
        # tgt: (batch_size, max_tgt_len)
        # dec_state: tuple with 2 tensors
        # encoder_outputs: (batch_size, max_src_len, hidden_size)
        # src_lengths: (batch_size)

        if dec_state[0].shape[0] == 2:
            dec_state = reshape_state(dec_state)

        # 1. Remove <sos> for decoder input
        if tgt.size(1) > 1:
            tgt = tgt[:, :-1]

        # 2. Embed the target sequence
        embedded = self.embedding(tgt)  # (batch_size, max_tgt_len, hidden_size)
        embedded = self.dropout(embedded)

        outputs = []
        for t in range(tgt.size(1)):  # max_tgt_len
            # 3.1. Pass through LSTM
            output, dec_state = self.lstm(embedded[:, t:t + 1, :], dec_state)  # (batch_size, 1, hidden_size)

            # 3.2. Apply attention if specified
            if self.attn is not None:
                output = self.attn(output, encoder_outputs, src_lengths)

            outputs.append(output)

        # 4. Concatenate outputs
        outputs = torch.cat(outputs, dim=1)  # (batch_size, max_tgt_len, hidden_size)

        return outputs, dec_state


class Seq2Seq(nn.Module):
    def __init__(
        self,
        encoder,
        decoder,
    ):
        super(Seq2Seq, self).__init__()

        self.encoder = encoder
        self.decoder = decoder

        self.generator = nn.Linear(decoder.hidden_size, decoder.tgt_vocab_size)

        self.generator.weight = self.decoder.embedding.weight

    def forward(
        self,
        src,
        src_lengths,
        tgt,
        dec_hidden=None,
    ):

        encoder_outputs, final_enc_state = self.encoder(src, src_lengths)

        if dec_hidden is None:
            dec_hidden = final_enc_state

        output, dec_hidden = self.decoder(
            tgt, dec_hidden, encoder_outputs, src_lengths
        )

        return self.generator(output), dec_hidden
