namespace ItSupport.Api.Answers;

public interface IAnswerProvider
{
    Task<Answer> AnswerAsync(string question, CancellationToken cancellationToken = default);
}
