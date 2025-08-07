vim.api.nvim_create_autocmd("TermOpen", {
	callback = function()
		local buf = vim.api.nvim_get_current_buf()
		vim.keymap.set("t", "<c-u>", function()
			local job_id = vim.b.terminal_job_id
			if job_id then
				vim.api.nvim_chan_send(job_id, "<DEBUG ultrathink=1>IGNORE</DEBUG>")
			end
		end, { buffer = buf, desc = "Send debug ultrathink command" })
	end,
	desc = "Set up terminal debug mapping",
})
return {}
